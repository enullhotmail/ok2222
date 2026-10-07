import os
import shutil
import asyncio
import json
import base64
import requests
import time
import uuid
import urllib.parse
import io
import tempfile
import random
import html
from datetime import datetime
import redis.asyncio as redis 
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
import logging

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "http://localhost:8080")
ADMIN_IDS = [7701391471, 8743187576]

PHONE, OTP, ASK_NAME = range(3)

executor = ThreadPoolExecutor(max_workers=5)

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Mobile Safari/537.36"
]

def is_admin(user_id):
    return user_id in ADMIN_IDS

# حداکثر طول متن پاسخ سرور که برای کاربر نمایش داده می‌شود (برای جلوگیری از پیام‌های بیش از حد طولانی)
MAX_RESPONSE_PREVIEW = 1500

def format_server_response(response) -> str:
    """پاسخ خام سرور (کد وضعیت + بدنه) را برای نمایش امن در تلگرام فرمت می‌کند."""
    try:
        body = response.text
    except Exception:
        body = ""
    if len(body) > MAX_RESPONSE_PREVIEW:
        body = body[:MAX_RESPONSE_PREVIEW] + "\n... (کوتاه‌شده)"
    body_safe = html.escape(body) if body else "(بدون محتوا)"
    return f"📡 <b>کد وضعیت:</b> <code>{response.status_code}</code>\n📄 <b>پاسخ سرور:</b>\n<pre>{body_safe}</pre>"

# ==========================================
# سیستم امنیتی و سهمیه ثابت (مادام‌العمر) کاربران
# ==========================================
async def is_authorized(user_id):
    if is_admin(user_id): return True
    limit = await redis_client.get(f"quota_limit:{user_id}")
    return limit is not None

async def check_user_quota(user_id):
    if is_admin(user_id): return True, "نامحدود", 0
    limit = await redis_client.get(f"quota_limit:{user_id}")
    if not limit: return False, 0, 0
    limit = int(limit)
    used_key = f"quota_used:{user_id}"
    used = await redis_client.get(used_key)
    used = int(used) if used else 0
    if used >= limit: return False, limit, used
    return True, limit, used

async def increment_user_quota(user_id):
    if is_admin(user_id): return
    used_key = f"quota_used:{user_id}"
    await redis_client.incr(used_key)

# ==========================================
# سیستم مدیریت پروکسی و API
# ==========================================
async def get_random_proxy_from_db():
    proxies_json = await redis_client.get("settings:proxies")
    if proxies_json:
        proxies = json.loads(proxies_json)
        if proxies and len(proxies) > 0:
            p = random.choice(proxies)
            return {"http": p, "https": p}
    return None

def get_user_id_from_token(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        decoded_bytes = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded_bytes)
        uid = data.get('cerberusId') or data.get('alternativeCustomerId') or data.get('userId')
        return int(uid) if uid else 0
    except Exception: return 0

def update_tokens_in_data(data, old_acc, new_acc, old_ref, new_ref):
    try:
        content = json.dumps(data, ensure_ascii=False)
        if old_acc and new_acc: content = content.replace(old_acc, new_acc)
        if old_ref and new_ref: content = content.replace(old_ref, new_ref)
        return json.loads(content)
    except Exception: return data

class OkalaAPI:
    def __init__(self):
        self.request_logs = []
        self.base_headers = {
            'accept': 'application/json, text/plain, */*',
            'source': 'okala',
            'ui-version': '2.0',
            'origin': 'https://www.okala.com',
            'User-Agent': random.choice(USER_AGENTS),
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
        }

    def log_request(self, method, url, status_code, response_text):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.request_logs.append(f"[{timestamp}] {method} {url}\nStatus: {status_code}\nResponse: {response_text}\n{'-'*50}\n")

    def make_request(self, method, url, access_token=None, proxy_dict=None, **kwargs):
        headers = self.base_headers.copy()
        headers['X-Correlation-Id'] = str(uuid.uuid4())
        headers['X-User-Unique-Id'] = str(uuid.uuid4())
        headers['session-id'] = str(uuid.uuid4())
        if access_token: headers['Authorization'] = f'Bearer {access_token}'
        if 'headers' in kwargs: headers.update(kwargs.pop('headers'))

        for attempt in range(3):
            try:
                time.sleep(random.uniform(1.0, 2.5))
                res = requests.request(method, url, headers=headers, proxies=proxy_dict, timeout=25, **kwargs)
                self.log_request(method, url, res.status_code, res.text)
                if res.status_code == 200:
                    try: return 200, res.json()
                    except: return 200, {}
                elif res.status_code == 401: return 401, {}
                else: return res.status_code, res.text 
            except Exception as e:
                self.log_request(method, url, "EXCEPTION", str(e))
                time.sleep(1)
        return 0, "Network Error"

    def check_discount_api(self, token, uid, proxy_dict=None):
        url = f"https://apigateway.okala.com/api/discount/v1/discounts/customer/{uid}"
        return self.make_request('GET', url, access_token=token, proxy_dict=proxy_dict)

    def refresh_token(self, refresh_token, proxy_dict=None):
        url = "https://apigateway.okala.com/api/v1/accounts/tokens"
        data = {"grant_type": "refresh_token", "client_id": "customer_client_id", "client_secret": "u_M{'57j!%LI21#", "scope": "offline_access", "refresh_token": refresh_token}
        status, response_data = self.make_request('POST', url, headers={"content-type": "application/x-www-form-urlencoded"}, data=data, proxy_dict=proxy_dict)
        if status == 200 and isinstance(response_data, dict):
            return response_data.get('access_token'), response_data.get('refresh_token')
        return None, None

async def process_discounts_and_send_report(bot, chat_id, acc_keys):
    loop = asyncio.get_running_loop()
    api = OkalaAPI()
    ts = int(time.time())

    proxy_check = await get_random_proxy_from_db()
    if not proxy_check:
        await bot.send_message(chat_id=chat_id, text="⚠️ هیچ پروکسی‌ای در سیستم تنظیم نشده است!\nبررسی ادامه می‌یابد...", parse_mode='HTML')

    raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
    phone_to_latest_link = {}
    for item in raw_logs:
        try:
            entry = json.loads(item)
            phone_to_latest_link[entry['phone']] = entry['link']
        except: pass

    total = len(acc_keys)
    progress_msg = await bot.send_message(chat_id=chat_id, text=f"🔍 شروع بررسی <b>{total}</b> اکانت...\n⏳ لطفاً منتظر بمانید.", parse_mode='HTML')

    detail_logs = []

    def _check_sync(acc_token, ref_token, uid, p_dict, phone):
        proxy_ip = p_dict['http'].split('@')[-1].split(':')[0] if p_dict else "بدون پروکسی"
        log_line = f"[{time.strftime('%H:%M:%S')}] 📱 {phone} | UUID: {uid} | پروکسی: {proxy_ip}\n"
        status, res = api.check_discount_api(acc_token, uid, proxy_dict=p_dict)
        if status == 401 and ref_token:
            new_acc, new_ref = api.refresh_token(ref_token, proxy_dict=p_dict)
            if new_acc:
                status, res = api.check_discount_api(new_acc, uid, proxy_dict=p_dict)
                return status, res, new_acc, new_ref, log_line + "✅ رفرش موفق\n"
        if status == 200 and isinstance(res, dict):
            vouchers = res.get('data', [])
            if vouchers:
                log_line += f"  🎁 تخفیف یافت شد: {len(vouchers)} کد\n"
        return status, res, None, None, log_line

    discount_results = []
    done = 0

    for key in acc_keys:
        try:
            phone = key.replace("account:", "")
            token_data = await redis_client.hgetall(key)
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            if not access_token:
                done += 1; continue
            user_uuid = get_user_id_from_token(access_token)
            if not user_uuid:
                done += 1; continue

            proxy_dict = await get_random_proxy_from_db()
            status, res, new_acc, new_ref, log_line = await loop.run_in_executor(executor, _check_sync, access_token, refresh_token, user_uuid, proxy_dict, phone)
            detail_logs.append(log_line)

            if new_acc:
                await redis_client.hset(key, mapping={"access_token": new_acc, "refresh_token": new_ref or ""})

            if status == 200 and isinstance(res, dict):
                vouchers = res.get('data', [])
                if vouchers:
                    amounts = [v.get('discountAmount', 0) for v in vouchers if v.get('discountAmount')]
                    max_amount = max(amounts) // 10000 if amounts else 0
                    old_link = phone_to_latest_link.get(phone, "")
                    discount_results.append({"phone": phone, "count": len(vouchers), "max_amount": max_amount, "link": old_link})

            done += 1
            if done % 5 == 0 or done == total:
                try: await progress_msg.edit_text(f"🔍 بررسی اکانت‌ها...\n✅ انجام شده: <b>{done}/{total}</b>\n🎁 دارای تخفیف تاکنون: <b>{len(discount_results)}</b>", parse_mode='HTML')
                except Exception: pass
        except Exception: pass

    if discount_results:
        report_text = f"🎁 <b>گزارش تخفیف‌ها ({len(discount_results)} اکانت از {total}):</b>\n\n"
        for r in discount_results:
            link_line = f"🔗 {r['link']}" if r['link'] else "⚠️ لینک ثبت‌شده‌ای یافت نشد"
            report_text += f"📱 شماره: <code>{r['phone']}</code>\n🎟 تعداد: <b>{r['count']}</b> | مبلغ: <b>{r['max_amount']}هزار تومان</b>\n{link_line}\n{'─'*30}\n"
    else:
        report_text = f"➖ <b>هیچ تخفیفی یافت نشد.</b>\nبررسی‌شده: {total}"

    try: await progress_msg.delete()
    except Exception: pass

    try:
        if len(report_text) > 4000:
            file_out = io.BytesIO(report_text.encode('utf-8'))
            await bot.send_document(chat_id=chat_id, document=file_out, filename=f"Discounts_Report_{ts}.txt", caption=f"✅ گزارش کامل تخفیف‌ها")
        else:
            await bot.send_message(chat_id=chat_id, text=report_text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception: pass

def format_for_injector(auth_data):
    access_token = auth_data.get("access_token", "")
    refresh_token = auth_data.get("refresh_token", "")
    user_info = auth_data.get("UserInfo", {})
    
    user_dict = {
        "id": user_info.get("Id", 0), "alternativeId": user_info.get("AlternativeId", ""), "alternativeCustomerId": user_info.get("AlternativeCustomerId", 0),
        "firstName": user_info.get("FirstName", ""), "lastName": user_info.get("LastName", ""), "birthDate": "", "genderCode": user_info.get("GenderCode", 1),
        "emailAddress": user_info.get("EmailAddress", ""), "userName": user_info.get("UserName", ""), "mobilePhone": user_info.get("MobilePhone", ""),
        "stateCode": user_info.get("StateCode", 1), "customerIsLoggedInForFirstTime": user_info.get("CustomerIsLoggedInForFirstTime", False),
        "firstLoginDateTime": user_info.get("FirstLoginDateTime", ""), "state": user_info.get("State", False),
        "hasAddress": user_info.get("HasAddress", False), "birthDateEpoch": user_info.get("BirthDateEpoch", 0)
    }
    
    user_url_encoded = urllib.parse.quote(json.dumps(user_dict, ensure_ascii=False))
    persist_user_inner = user_dict.copy()
    persist_user_inner["token"] = access_token
    
    persist_root_dict = {
        "user": json.dumps({"user": persist_user_inner, "discountCode": None}, ensure_ascii=False),
        "cart": json.dumps({"cartData": [], "totalCartsCount": 0, "showDrawer": False, "cartTotalPrice": 0}),
        "mapInfo": json.dumps({"defaultViewPort": {"latitude": 35.69976, "longitude": 51.33808, "id": 129, "name": "تهران"}, "viewport": {"latitude": 35.69976, "longitude": 51.33808}, "selectedCity": {"id": 129, "name": "تهران", "lat": 35.69975, "lng": 51.33551}, "mapCityName": "تهران"}, ensure_ascii=False),
        "eventData": json.dumps({"isLoggedIn": True, "platform": "web", "viewedLayersCount": 0, "activeDiscountCodesCount": 0, "sessionLayersViewedCount": 0}),
        "_persist": json.dumps({"version": -1, "rehydrated": True})
    }
    persist_root_str = json.dumps(persist_root_dict, ensure_ascii=False)
    
    return {
        "cookies": [
            {"name": "tokenMS", "value": access_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"},
            {"name": "token", "value": access_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"},
            {"name": "refresh_token", "value": refresh_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"}
        ],
        "origins": [{
            "origin": "https://www.okala.com",
            "localStorage": [
                {"name": "tokenMS", "value": access_token}, {"name": "user", "value": user_url_encoded},
                {"name": "city_name", "value": "تهران"}, {"name": "city_id", "value": "129"},
                {"name": "persist:root", "value": persist_root_str}
            ]
        }]
    }

# ==========================================
# پردازش فایل‌های ادمین (پروکسی و ZIP)
# ==========================================
async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    file_name = update.message.document.file_name.lower()
    if not file_name.endswith('.zip'):
        await update.message.reply_text("❌ فایل ارسالی نامعتبر است. لطفاً فایل زیپ (.zip) ارسال کنید.")
        return
        
    action = context.user_data.get('admin_zip_action', 'zip_to_link')
    msg = await update.message.reply_text("⏳ در حال دریافت و استخراج فایل...")
    
    new_file = await update.message.document.get_file()
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "uploaded.zip")
        await new_file.download_to_drive(zip_path)
        extracted_dir = os.path.join(temp_dir, "extracted")
        await asyncio.to_thread(shutil.unpack_archive, zip_path, extracted_dir)
        
        json_files_paths = []
        for root, dirs, files in os.walk(extracted_dir):
            for file in files:
                if file.lower().endswith('.json'):
                    json_files_paths.append(os.path.join(root, file))
                    
        if not json_files_paths:
            await msg.edit_text("⚠️ هیچ فایل JSON معتبری در فایل زیپ یافت نشد.")
            return

        if action == 'zip_to_link':
            links_text = "<b>لیست لینک‌های تولید شده:</b>\n\n"
            count = 0
            for file_path in json_files_paths:
                filename = os.path.basename(file_path)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        data = json.loads(file_content)
                        access_token, refresh_token = None, None
                        for cookie in data.get('cookies', []):
                            if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                            elif cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
                        if not access_token:
                            for origin in data.get('origins', []):
                                for item in origin.get('localStorage', []):
                                    if item.get('name') == 'tokenMS': access_token = item.get('value')
                                    elif item.get('name') == 'refresh_token': refresh_token = item.get('value')
                        phone = filename.replace('.json', '')
                        if access_token and not await redis_client.exists(f"account:{phone}"):
                            await redis_client.hset(f"account:{phone}", mapping={"access_token": access_token, "refresh_token": refresh_token or ""})
                        link_id = str(uuid.uuid4())[:12]
                        # لینک‌ها برای همیشه و بدون انقضا ذخیره می‌شوند
                        await redis_client.set(f"acc_link:{link_id}", file_content)
                        final_url = f"{WEB_DOMAIN}/acc/{link_id}"
                        links_text += f"📱 <b>شماره {phone}:</b>\n{final_url}\n\n"
                        count += 1
                except Exception: pass
            if len(links_text) > 4000:
                file_out = io.BytesIO(links_text.encode('utf-8'))
                await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Links_{int(time.time())}.txt", caption=f"✅ استخراج {count} اکانت انجام شد.")
                await msg.delete()
            else:
                await msg.edit_text(f"✅ <b>تعداد {count} اکانت ذخیره شد:</b>\n\n{links_text}", disable_web_page_preview=True, parse_mode='HTML')

        elif action == 'zip_discount_check':
            await msg.edit_text("🔍 در حال بررسی وضعیت تخفیف‌ها با سیستم ضدربات و رفرش‌توکن. لطفاً منتظر بمانید...")
            discount_dir = os.path.join(temp_dir, "Discount_Accounts")
            os.makedirs(os.path.join(discount_dir, 'accounts'), exist_ok=True)
            links_text = "<b>لیست لینک‌های دارای تخفیف:</b>\n\n"
            discount_count = 0
            
            api = OkalaAPI()
            loop = asyncio.get_running_loop()
            raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
            phone_to_latest_link = {}
            for item in raw_logs:
                try:
                    entry = json.loads(item)
                    phone_to_latest_link[entry['phone']] = entry['link']
                except: pass
            
            def _check_sync_zip(acc_token, ref_token, uid, p_dict):
                status, res = api.check_discount_api(acc_token, uid, proxy_dict=p_dict)
                if status == 401 and ref_token:
                    new_acc, new_ref = api.refresh_token(ref_token, proxy_dict=p_dict)
                    if new_acc:
                        status, res = api.check_discount_api(new_acc, uid, proxy_dict=p_dict)
                        return status, res, new_acc, new_ref
                return status, res, None, None
            
            for file_path in json_files_paths:
                filename = os.path.basename(file_path)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        data = json.loads(file_content)
                        access_token = None
                        refresh_token = None
                        phone = filename.replace('.json', '')
                        for cookie in data.get('cookies', []):
                            if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                            if cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
                        if not access_token:
                            for origin in data.get('origins', []):
                                for item in origin.get('localStorage', []):
                                    if item.get('name') == 'tokenMS': access_token = item.get('value')
                                    if item.get('name') == 'refresh_token': refresh_token = item.get('value')
                        
                        if access_token:
                            user_uuid = get_user_id_from_token(access_token)
                            if user_uuid:
                                proxy_dict = await get_random_proxy_from_db()
                                status, res, new_acc, new_ref = await loop.run_in_executor(executor, _check_sync_zip, access_token, refresh_token, user_uuid, proxy_dict)
                                
                                if new_acc:
                                    data = update_tokens_in_data(data, access_token, new_acc, refresh_token, new_ref)
                                    file_content = json.dumps(data, ensure_ascii=False)
                                    with open(file_path, 'w', encoding='utf-8') as fw:
                                        fw.write(file_content)

                                if status == 200 and isinstance(res, dict):
                                    vouchers = res.get('data', [])
                                    if vouchers:
                                        discount_count += 1
                                        shutil.copy2(file_path, os.path.join(discount_dir, 'accounts', filename))
                                        old_link = phone_to_latest_link.get(phone, "لینک قدیمی در دیتابیس یافت نشد")
                                        links_text += f"📱 <b>شماره {phone}:</b>\n{old_link}\n\n"
                except Exception as e:
                    api.request_logs.append(f"[{filename}] Exception: {str(e)}\n{'-'*40}\n")
                    
            if discount_count > 0:
                discount_zip_path = os.path.join(temp_dir, "Discounted_Accounts")
                await asyncio.to_thread(shutil.make_archive, discount_zip_path, 'zip', discount_dir)
                await msg.delete()
                with open(discount_zip_path + '.zip', 'rb') as zip_file:
                    await context.bot.send_document(chat_id=user_id, document=zip_file, filename="Discounted_Accounts.zip", caption=f"🎁 <b>فایل خروجی (فیلتر شده)</b>\nتعداد اکانت‌های دارای تخفیف: {discount_count}", parse_mode='HTML')
                if len(links_text) > 4000:
                    file_out = io.BytesIO(links_text.encode('utf-8'))
                    await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Discount_Links_{int(time.time())}.txt", caption="🔗 لینک‌های دسترسی سریع")
                else:
                    await context.bot.send_message(chat_id=user_id, text=links_text, disable_web_page_preview=True, parse_mode='HTML')
            else:
                await msg.edit_text("⚠️ هیچ‌یک از اکانت‌های موجود دارای تخفیف نبودند.")

async def handle_admin_text_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    
    state = context.user_data.get('admin_state')
    if state == 'waiting_for_proxy':
        msg = await update.message.reply_text("⏳ در حال خواندن پروکسی‌ها...")
        try:
            if update.message.document:
                file_name = update.message.document.file_name.lower()
                if not file_name.endswith('.txt'):
                    await msg.edit_text("❌ فرمت فایل پروکسی باید `.txt` باشد.")
                    return
                file = await update.message.document.get_file()
                with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                    await file.download_to_drive(temp_file.name)
                    with open(temp_file.name, 'r', encoding='utf-8') as f:
                        text_content = f.read()
            else:
                text_content = update.message.text
                
            proxies = []
            for line in text_content.split('\n'):
                line = line.strip()
                if line:
                    if not line.startswith('http') and not line.startswith('socks'):
                        line = f"http://{line}"
                    proxies.append(line)
                    
            if proxies:
                await redis_client.set("settings:proxies", json.dumps(proxies))
                context.user_data['admin_state'] = None
                await msg.edit_text(f"✅ تعداد <b>{len(proxies)}</b> پروکسی با موفقیت ذخیره و تنظیم شد.", reply_markup=get_admin_keyboard(), parse_mode='HTML')
            else:
                await msg.edit_text("⚠️ متنی حاوی پروکسی یافت نشد. لطفاً مجدداً امتحان کنید.")
        except Exception as e:
            await msg.edit_text("❌ خطا در پردازش فایل یا متن پروکسی.")

# ==========================================
# مینی‌سرور وب
# ==========================================
async def web_handler_get_account(request):
    link_id = request.match_info.get('link_id', '')
    data = await redis_client.get(f"acc_link:{link_id}")
    if data: return web.json_response(json.loads(data))
    return web.json_response({"error": "لینک نامعتبر است."}, status=404)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/acc/{link_id}', web_handler_get_account)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080)) 
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# ==========================================
# دکمه‌ها و منوها
# ==========================================
def get_main_keyboard(is_admin_user):
    keyboard = [
        [InlineKeyboardButton("🔑 ورود به حساب (ساخت لینک)", callback_data="user_login")],
        [InlineKeyboardButton("📂 دریافت تمام لینک‌های من", callback_data="get_my_links")]
    ]
    if is_admin_user:
        keyboard.append([InlineKeyboardButton("⚙️ پنل مدیریت (ادمین)", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 آمار دیتابیس", callback_data="admin_stats")],
        [InlineKeyboardButton("📋 گزارش لینک‌های کاربران", callback_data="admin_users_report")],
        [InlineKeyboardButton("🎁 بررسی تخفیف دیتابیس", callback_data="admin_check_discounts")],
        [InlineKeyboardButton("🔗 تبدیل زیپ به لینک", callback_data="admin_zip_to_link"), InlineKeyboardButton("🔍 بررسی تخفیف زیپ", callback_data="admin_zip_discount")],
        [InlineKeyboardButton("📥 استخراج شماره‌ها", callback_data="admin_export"), InlineKeyboardButton("🗑 پاکسازی دیتابیس", callback_data="admin_clear")],
        [InlineKeyboardButton("🔗 استخراج لینک‌ها", callback_data="admin_export_links"), InlineKeyboardButton("🔑 استخراج توکن‌ها", callback_data="admin_export_tokens")],
        [InlineKeyboardButton("🛠 تعمیر خودکار لینک‌های ناقص", callback_data="admin_repair_links")],
        [InlineKeyboardButton("🌐 تنظیم پروکسی", callback_data="admin_set_proxy")], 
        [InlineKeyboardButton("⏸ تغییر وضعیت ربات (روشن/خاموش)", callback_data="admin_toggle")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not await is_authorized(user_id):
        text = f"⛔️ <b>دسترسی غیرمجاز!</b>\n\nشما مجوز استفاده از این سیستم را ندارید.\nجهت دریافت دسترسی، آیدی عددی زیر را کپی کرده و به مدیریت ارسال کنید:\n\n<code>{user_id}</code>"
        if update.message: await update.message.reply_text(text, parse_mode='HTML')
        else:
            try: await update.callback_query.edit_message_text(text, parse_mode='HTML')
            except: pass
        return

    admin_status = is_admin(user_id)
    has_quota, limit, used = await check_user_quota(user_id)
    
    text = f"👋 <b>به سیستم لینک‌ساز خوش آمدید.</b>\n\n"
    if not admin_status:
        text += f"📊 <b>وضعیت حساب شما:</b>\n"
        text += f"▫️ کل سهمیه اختصاص یافته: <b>{limit}</b> لینک\n"
        text += f"▫️ لینک‌های ساخته شده: <b>{used}</b> لینک\n"
        text += f"▫️ باقیمانده مجاز: <b>{limit - used}</b> لینک\n\n"
            
    text += "لطفاً یک گزینه را انتخاب کنید:"
    
    if update.message: await update.message.reply_text(text, reply_markup=get_main_keyboard(admin_status), parse_mode='HTML')
    else:
        try: await update.callback_query.edit_message_text(text, reply_markup=get_main_keyboard(admin_status), parse_mode='HTML')
        except Exception: pass

# ================= دستورات مدیریت دسترسی =================
async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        if len(context.args) < 2:
            await update.message.reply_text("❌ فرمت صحیح دستور:\n`/adduser [USER_ID] [COUNT]`\nمثال:\n`/adduser 123456789 500`", parse_mode="Markdown")
            return
        target_user = context.args[0]
        limit_count = int(context.args[1])
        await redis_client.set(f"quota_limit:{target_user}", limit_count)
        await update.message.reply_text(f"✅ دسترسی کاربر <code>{target_user}</code> تنظیم شد.\nکل سهمیه مجاز: <b>{limit_count}</b> لینک.", parse_mode="HTML")
    except Exception as e: await update.message.reply_text(f"❌ خطا: {str(e)}")

async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        if len(context.args) < 1:
            await update.message.reply_text("❌ فرمت صحیح:\n`/deluser [USER_ID]`", parse_mode="Markdown")
            return
        target_user = context.args[0]
        await redis_client.delete(f"quota_limit:{target_user}")
        await update.message.reply_text(f"🗑 دسترسی کاربر <code>{target_user}</code> لغو شد.", parse_mode="HTML")
    except Exception as e: await update.message.reply_text(f"❌ خطا: {str(e)}")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    context.user_data['admin_state'] = None
    context.user_data['admin_zip_action'] = None
    await update.message.reply_text("⚙️ <b>پنل مدیریت سیستم:</b>", reply_markup=get_admin_keyboard(), parse_mode='HTML')

# ================= مدیریت Callback ها (همه دکمه‌ها) =================
async def core_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not await is_authorized(user_id):
        await query.answer("⛔️ دسترسی شما به ربات مسدود است.", show_alert=True)
        return

    data = query.data
    
    if data == "main_menu":
        await query.answer()
        context.user_data['admin_state'] = None
        await show_main_menu(update, context)
        return
        
    if data == "get_my_links":
        await query.answer("در حال دریافت لینک‌ها... ⏳")
        user_links_raw = await redis_client.lrange(f"user_links_history:{user_id}", 0, -1)
        if not user_links_raw:
            await query.edit_message_text("⚠️ شما هنوز هیچ لینکی نساخته‌اید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]]))
            return
        report_text = f"📂 <b>تمامی لینک‌های ساخته شده توسط شما (تعداد: {len(user_links_raw)}):</b>\n\n"
        for idx, item_str in enumerate(user_links_raw, 1):
            try:
                item = json.loads(item_str)
                report_text += f"{idx}. 📱 <code>{item['phone']}</code>\n🕒 {item.get('date', 'نامشخص')}\n🔗 {item['link']}\n\n"
            except: pass
            
        if len(report_text) > 4000:
            file_out = io.BytesIO(report_text.encode('utf-8'))
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"My_Generated_Links.txt", caption="📂 فایل حاوی تمام لینک‌های شما")
            await show_main_menu(update, context)
        else:
            await query.edit_message_text(report_text, disable_web_page_preview=True, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]]))
        return

    if data == "finish_link_creation":
        await query.answer("در حال آماده‌سازی لینک‌ها... ⏳")
        session_links = context.user_data.get('session_links', [])
        if not session_links:
            await query.edit_message_text("⚠️ هیچ لینکی ساخته نشده است.", reply_markup=get_main_keyboard(is_admin(user_id)))
            return
        report_text = f"🎉 <b>لینک‌های تولید شده در این نوبت (تعداد: {len(session_links)}):</b>\n\n"
        for idx, item in enumerate(session_links, 1): report_text += f"{idx}. 📱 <code>{item['phone']}</code>\n🔗 {item['link']}\n\n"
        context.user_data['session_links'] = []
        if len(report_text) > 4000:
            file_out = io.BytesIO(report_text.encode('utf-8'))
            await context.bot.send_document(chat_id=user_id, document=file_out, filename="Session_Links.txt")
            await show_main_menu(update, context)
        else:
            await query.edit_message_text(report_text, disable_web_page_preview=True, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]]))
        return

    # --- توابع مدیریت ادمین ---
    if not is_admin(user_id): return
    await query.answer()
    
    if data == "admin_panel":
        context.user_data['admin_zip_action'] = None
        await query.edit_message_text("⚙️ <b>پنل مدیریت سیستم:</b>", reply_markup=get_admin_keyboard(), parse_mode='HTML')
        
    elif data == "admin_stats":
        acc_keys = await redis_client.keys("account:*")
        link_keys = await redis_client.keys("acc_link:*")
        proxies_json = await redis_client.get("settings:proxies")
        proxy_count = len(json.loads(proxies_json)) if proxies_json else 0
        maint = await redis_client.get("settings:maintenance")
        text = (f"📊 <b>وضعیت سیستم:</b>\n\n👤 <b>تعداد کل اکانت‌ها:</b> <code>{len(acc_keys)}</code>\n"
                f"🔗 <b>لینک‌های فعال در دیتابیس:</b> <code>{len(link_keys)}</code>\n🌐 <b>تعداد پروکسی‌ها:</b> <code>{proxy_count}</code>\n"
                f"🤖 <b>وضعیت ربات:</b> {'غیرفعال 🔴' if maint == '1' else 'فعال 🟢'}")
        await query.edit_message_text(text, reply_markup=get_admin_keyboard(), parse_mode='HTML')

    elif data == "admin_users_report":
        raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
        if not raw_logs:
            await context.bot.send_message(chat_id=user_id, text="⚠️ گزارشی ثبت نشده است.")
            return
        users_data = {}
        for item in raw_logs:
            try:
                entry = json.loads(item)
                uid = entry.get("tg_id")
                if uid not in users_data: users_data[uid] = {"name": entry.get("tg_name", "نامشخص"), "username": entry.get("tg_user", ""), "links": []}
                users_data[uid]["links"].append(entry)
            except: pass
        report_text = "📊 <b>گزارش جامع ساخت لینک کاربران:</b>\n\n"
        for uid, udata in users_data.items():
            uname = f" (@{udata['username']})" if udata['username'] else ""
            report_text += f"👤 کاربر: {udata['name']}{uname}\n🆔 آیدی: <code>{uid}</code>\n🔢 تعداد کل لینک‌ها: {len(udata['links'])}\n------------------------\n"
        file_out = io.BytesIO(report_text.encode('utf-8'))
        await context.bot.send_document(chat_id=user_id, document=file_out, filename="Users_Summary.txt", caption="📊 گزارش خلاصه کارکرد کاربران")

    elif data == "admin_check_discounts":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ دیتابیس خالی است.")
            return
        asyncio.create_task(process_discounts_and_send_report(context.bot, user_id, acc_keys))

    elif data == "admin_zip_to_link":
        context.user_data['admin_zip_action'] = 'zip_to_link'
        await context.bot.send_message(chat_id=user_id, text="🔗 <b>عملیات استخراج لینک:</b>\nلطفاً فایل ZIP را ارسال کنید.", parse_mode='HTML')
        
    elif data == "admin_zip_discount":
        context.user_data['admin_zip_action'] = 'zip_discount_check'
        await context.bot.send_message(chat_id=user_id, text="🔍 <b>عملیات بررسی تخفیف:</b>\nلطفاً فایل ZIP را ارسال کنید.", parse_mode='HTML')

    elif data == "admin_export":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ دیتابیس خالی است.")
            return
        export_text = "لیست شماره‌های ثبت شده در سیستم:\n\n" + "\n".join([k.replace('account:', '') for k in acc_keys])
        await context.bot.send_document(chat_id=user_id, document=io.BytesIO(export_text.encode('utf-8')), filename="Accounts.txt")

    elif data == "admin_export_links":
        link_keys = await redis_client.keys("acc_link:*")
        if not link_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ هیچ لینکی موجود نیست.")
            return
        export_text = "لیست لینک‌های موجود در دیتابیس:\n\n"
        for l_key in link_keys:
            link_id = l_key.replace("acc_link:", "")
            final_url = f"{WEB_DOMAIN}/acc/{link_id}"
            link_data = await redis_client.get(l_key)
            phone = "نامشخص"
            try:
                data_json = json.loads(link_data)
                for item in data_json.get("origins", [])[0].get("localStorage", []):
                    if item.get("name") == "user":
                        user_obj = json.loads(urllib.parse.unquote(item.get("value")))
                        phone = user_obj.get("mobilePhone", "نامشخص")
                        break
            except: pass
            export_text += f"📱 شماره: {phone}\n🔗 لینک: {final_url}\n\n"
        await context.bot.send_document(chat_id=user_id, document=io.BytesIO(export_text.encode('utf-8')), filename="Links_With_Phone.txt")

    elif data == "admin_export_tokens":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys: return
        exported_data = {}
        for key in acc_keys:
            tokens = await redis_client.hgetall(key)
            exported_data[key.replace('account:', '')] = {"access_token": tokens.get("access_token", ""), "refresh_token": tokens.get("refresh_token", "")}
        await context.bot.send_document(chat_id=user_id, document=io.BytesIO(json.dumps(exported_data, indent=4, ensure_ascii=False).encode('utf-8')), filename="Tokens_DB.json")

    elif data == "admin_repair_links":
        link_keys = await redis_client.keys("acc_link:*")
        msg = await context.bot.send_message(chat_id=user_id, text="🛠 در حال بررسی و تعمیر لینک‌ها...")
        repaired_count = 0
        for l_key in link_keys:
            try:
                data_json = json.loads(await redis_client.get(l_key))
                phone = None
                for item in data_json.get("origins", [])[0].get("localStorage", []):
                    if item.get("name") == "user": phone = json.loads(urllib.parse.unquote(item.get("value"))).get("mobilePhone"); break
                if phone:
                    acc_data = await redis_client.hgetall(f"account:{phone}")
                    r_tok, t_ms = acc_data.get("refresh_token"), acc_data.get("access_token")
                    if r_tok and t_ms:
                        if "cookies" not in data_json: data_json["cookies"] = []
                        data_json["cookies"] = [c for c in data_json["cookies"] if c.get("name") not in ["tokenMS", "token", "refresh_token"]]
                        data_json["cookies"].extend([{"name": n, "value": v, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"} for n, v in [("tokenMS", t_ms), ("token", t_ms), ("refresh_token", r_tok)]])
                        # لینک‌ها را بدون انقضا ذخیره می‌کنیم
                        await redis_client.set(l_key, json.dumps(data_json, ensure_ascii=False))
                        repaired_count += 1
            except: pass
        await msg.edit_text(f"✅ عملیات پایان یافت.\n<b>{repaired_count}</b> لینک ترمیم شد.", parse_mode='HTML')

    elif data == "admin_clear":
        kb = [[InlineKeyboardButton("✅ تایید عملیات حذف", callback_data="admin_clear_confirm"), InlineKeyboardButton("❌ انصراف", callback_data="admin_panel")]]
        await query.edit_message_text("⚠️ <b>اخطار:</b> این عملیات کل دیتابیس (شماره‌ها) را حذف میکند. مطمئنید؟", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        
    elif data == "admin_clear_confirm":
        acc_keys = await redis_client.keys("account:*")
        if acc_keys: await redis_client.delete(*acc_keys)
        await query.edit_message_text("🗑 پاکسازی انجام شد.", reply_markup=get_admin_keyboard())
        
    elif data == "admin_toggle":
        current = await redis_client.get("settings:maintenance")
        new_val = "0" if current == "1" else "1"
        await redis_client.set("settings:maintenance", new_val)
        await query.edit_message_text(f"⚙️ <b>وضعیت ربات:</b> {'غیرفعال 🔴' if new_val == '1' else 'فعال 🟢'}", reply_markup=get_admin_keyboard(), parse_mode='HTML')

    elif data == "admin_set_proxy":
        context.user_data['admin_state'] = 'waiting_for_proxy'
        await query.edit_message_text("🌐 <b>تنظیم پروکسی‌ها:</b>\nمتن پروکسی‌ها یا فایل `.txt` را بفرستید.", parse_mode='HTML')

# ================= توابع لاگین کاربر با پروکسی و سیستم مدیریت خطا =================
def get_user_headers(context: ContextTypes.DEFAULT_TYPE):
    if 'device_id' not in context.user_data:
        context.user_data['device_id'] = str(uuid.uuid4())
        context.user_data['session_id'] = str(uuid.uuid4())
    headers = {'accept': 'application/json, text/plain, */*', 'source': 'okala', 'ui-version': '2.0', 'origin': 'https://www.okala.com', 'User-Agent': random.choice(USER_AGENTS)}
    headers['X-User-Unique-Id'] = context.user_data['device_id']
    headers['session-id'] = context.user_data['session_id']
    return headers

async def check_maintenance(update: Update) -> bool:
    maint = await redis_client.get("settings:maintenance")
    user_id = update.effective_user.id if update.effective_user else 0
    if maint == "1" and not is_admin(user_id):
        text = "⛔️ سیستم در حال حاضر موقتاً غیرفعال است."
        if update.message: await update.message.reply_text(text)
        else: await update.callback_query.message.reply_text(text)
        return True
    return False

async def start_login_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    user_id = update.effective_user.id
    
    if not await is_authorized(user_id):
        await update.callback_query.answer("⛔️ شما مجاز نیستید.", show_alert=True)
        return ConversationHandler.END

    has_quota, limit, used = await check_user_quota(user_id)
    if not has_quota:
        msg = f"سقف مجاز کل شما ({limit} لینک) به اتمام رسیده است." if limit > 0 else "شما دسترسی برای ساخت لینک ندارید."
        await update.callback_query.answer(msg, show_alert=True)
        return ConversationHandler.END

    await update.callback_query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
    await update.callback_query.edit_message_text("📱 <b>لطفاً شماره موبایل خود را وارد کنید:</b>", reply_markup=kb, parse_mode='HTML')
    return PHONE

async def cancel_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("عملیات لغو شد ❌")
    await show_main_menu(update, context) 
    return ConversationHandler.END

async def request_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    phone = update.message.text.strip()
    
    # سیستم محدودیت زمانی پیامک (Cooldown)
    ttl = await redis_client.ttl(f"otp_cooldown:{phone}")
    if ttl > 0:
        minutes, seconds = divmod(ttl, 60)
        time_str = f"{minutes} دقیقه و {seconds} ثانیه" if minutes > 0 else f"{seconds} ثانیه"
        await update.message.reply_text(f"⏳ پیامک به تازگی برای این شماره ارسال شده است.\nلطفاً {time_str} صبر کنید و سپس دوباره تلاش کنید.", parse_mode='HTML')
        return PHONE
        
    context.user_data['phone'] = phone
    msg = await update.message.reply_text("⏳ در حال ارتباط با سرور و ارسال پیامک...")
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {"mobile": phone, "deviceTypeCode": 7, "confirmTerms": True, "notRobot": False, "otpType": 0, "ValidationCodeCreateReason": 5, "OtpApp": 0, "IsAppOnly": False}
    proxy_dict = await get_random_proxy_from_db()
    
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            executor, 
            lambda: requests.post(url, json=payload, headers=get_user_headers(context), proxies=proxy_dict, timeout=15)
        )
        
        if response.status_code == 200:
            # اعمال محدودیت زمانی 2 دقیقه ای برای شماره
            await redis_client.setex(f"otp_cooldown:{phone}", 120, "1") 
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")], [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
            await msg.edit_text("✉️ <b>کد تایید ارسال شد.</b>\nلطفاً آن را وارد کنید:", reply_markup=kb, parse_mode='HTML')
            return OTP
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
            await msg.edit_text(f"❌ <b>خطا از سمت سیستم اکالا:</b>\n\n{format_server_response(response)}", reply_markup=kb, parse_mode='HTML')
            return ConversationHandler.END
            
    except Exception as e:
        logging.error(f"Error in OTP request: {e}")
        await msg.edit_text(f"❌ خطا در ارتباط با سرور (احتمالاً پروکسی قطع است یا IP مسدود شده).\n\n<b>جزئیات خطا:</b>\n<code>{html.escape(str(e))}</code>", parse_mode='HTML')
        return ConversationHandler.END

async def resend_otp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone')
    
    # چک کردن محدودیت زمانی قبل از ارسال مجدد
    ttl = await redis_client.ttl(f"otp_cooldown:{phone}")
    if ttl > 0:
        minutes, seconds = divmod(ttl, 60)
        time_str = f"{minutes} دقیقه و {seconds} ثانیه" if minutes > 0 else f"{seconds} ثانیه"
        await query.answer(f"⏳ لطفاً {time_str} دیگر صبر کنید.", show_alert=True)
        return OTP

    await query.answer("در حال ارسال مجدد کد... ⏳")
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {"mobile": phone, "deviceTypeCode": 7, "confirmTerms": True, "notRobot": False, "otpType": 0, "ValidationCodeCreateReason": 5, "OtpApp": 0, "IsAppOnly": False}
    proxy_dict = await get_random_proxy_from_db()
    
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            executor, 
            lambda: requests.post(url, json=payload, headers=get_user_headers(context), proxies=proxy_dict, timeout=15)
        )
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")], [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
        if response.status_code == 200:
            await redis_client.setex(f"otp_cooldown:{phone}", 120, "1")
            await query.edit_message_text(f"✉️ <b>کد تایید مجدداً ارسال شد.</b>\nکد را وارد کنید:", reply_markup=kb, parse_mode='HTML')
        else:
            await query.edit_message_text(f"❌ <b>خطا در ارسال مجدد:</b>\n\n{format_server_response(response)}", reply_markup=kb, parse_mode='HTML')
    except Exception as e:
        await query.edit_message_text(f"❌ خطا در ارتباط با سرور هنگام ارسال مجدد.\n\n<b>جزئیات خطا:</b>\n<code>{html.escape(str(e))}</code>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل", callback_data="cancel_action")]]), parse_mode='HTML')
        
    return OTP 

async def verify_otp_and_check_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    otp_code = update.message.text.strip()
    phone = context.user_data.get('phone')
    msg = await update.message.reply_text("⏳ در حال بررسی کد تایید...")
    
    token_url = "https://apigateway.okala.com/api/v1/accounts/tokens"
    payload = {"mobile_number": phone, "otp_code": otp_code, "grant_type": "customer_grant_type", "client_id": "customer_client_id", "client_secret": "u_M{'57j!%LI21#", "client_name": "customer_client_name", "device_type_code": 7, "scope": "offline_access", "loginDuration": 4815}
    headers = get_user_headers(context)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    proxy_dict = await get_random_proxy_from_db()
    
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            executor, 
            lambda: requests.post(token_url, data=payload, headers=headers, proxies=proxy_dict, timeout=15)
        )
        
        if response.status_code == 200:
            auth_data = response.json()
            context.user_data['auth_data'] = auth_data 
            if auth_data.get("access_token"):
                await redis_client.hset(f"account:{phone}", mapping={"access_token": auth_data.get("access_token"), "refresh_token": auth_data.get("refresh_token")})
            if not auth_data.get("UserInfo", {}).get("HasName", False):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
                await msg.edit_text("⚠️ <b>اطلاعات حساب ناقص است.</b>\nلطفاً نام و نام خانوادگی خود را وارد کنید:", reply_markup=kb, parse_mode='HTML')
                return ASK_NAME
            else: 
                return await generate_and_send_link(update, context, msg)
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")], [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
            await msg.edit_text(f"❌ <b>کد وارد شده اشتباه یا منقضی است.</b>\n\n{format_server_response(response)}", reply_markup=kb, parse_mode='HTML')
            return OTP 
            
    except Exception as e:
        logging.error(f"Error verifying OTP: {e}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل", callback_data="cancel_action")]])
        await msg.edit_text(f"❌ خطا در ارتباط با سرور. لطفاً مجدد تلاش کنید.\n\n<b>جزئیات خطا:</b>\n<code>{html.escape(str(e))}</code>", reply_markup=kb, parse_mode='HTML')
        return OTP

async def save_name_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    full_name = update.message.text.strip()
    if not full_name: return ASK_NAME
    parts = full_name.split(maxsplit=1)
    msg = await update.message.reply_text("⏳ در حال ثبت اطلاعات...")
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/UpdateCustomer" 
    headers = get_user_headers(context)
    headers["Authorization"] = f"Bearer {context.user_data['auth_data'].get('access_token')}"
    payload = {"birthDate": "", "birthDateEpoch": 700086600, "customerType": 0, "firstName": parts[0], "genderCode": 1, "genderTitle": "مذکر", "lastName": parts[1] if len(parts)>1 else "", "gender": "male"}
    proxy_dict = await get_random_proxy_from_db()
    
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            executor,
            lambda: requests.post(url, json=payload, headers=headers, proxies=proxy_dict, timeout=15)
        )
    except Exception:
        pass # خطا در تنظیم نام مانع ساخت لینک نمی‌شود
        
    return await generate_and_send_link(update, context, msg)

async def generate_and_send_link(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg) -> int:
    user_id = update.effective_user.id
    has_quota, limit, used = await check_user_quota(user_id)
    if not has_quota:
        await status_msg.edit_text("❌ کل سقف مجاز ساخت لینک شما به پایان رسید.")
        return ConversationHandler.END

    auth_data = context.user_data.get('auth_data')
    phone = context.user_data.get('phone', 'نامشخص')
    injection_json = format_for_injector(auth_data)
    link_id = str(uuid.uuid4())[:12]
    
    # لینک‌ها برای همیشه و بدون انقضا ذخیره می‌شوند
    await redis_client.set(f"acc_link:{link_id}", json.dumps(injection_json, ensure_ascii=False))
    final_url = f"{WEB_DOMAIN}/acc/{link_id}"
    
    if 'session_links' not in context.user_data: context.user_data['session_links'] = []
    context.user_data['session_links'].append({"phone": phone, "link": final_url})
    
    tg_user = update.effective_user
    log_entry = {"tg_id": tg_user.id, "tg_name": tg_user.full_name or "نامشخص", "tg_user": tg_user.username or "", "phone": phone, "link": final_url, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    await redis_client.rpush("global_link_logs", json.dumps(log_entry, ensure_ascii=False))
    
    user_link_entry = {"phone": phone, "link": final_url, "date": time.strftime("%Y-%m-%d %H:%M:%S")}
    await redis_client.rpush(f"user_links_history:{user_id}", json.dumps(user_link_entry, ensure_ascii=False))
    
    await increment_user_quota(user_id)
    count = len(context.user_data['session_links'])
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ساخت یک لینک دیگر", callback_data="user_login")],
        [InlineKeyboardButton("🏁 پایان ساخت", callback_data="finish_link_creation")],
        [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="main_menu")]
    ])
    text = (f"✅ <b>ورود به حساب شماره {phone} با موفقیت انجام شد.</b>\n\n"
            f"📥 لینک تولید شد و آماده تحویل است.\n📊 آماده تحویل در این نوبت: <b>{count}</b>")
    await status_msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ عملیات متوقف شد.")
    await show_main_menu(update, context)
    return ConversationHandler.END

async def main():
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable is missing.")
        return

    await start_web_server()
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler('start', show_main_menu))
    application.add_handler(CommandHandler('admin', admin_command))
    application.add_handler(CommandHandler('adduser', add_user_command))
    application.add_handler(CommandHandler('deluser', del_user_command))
    
    application.add_handler(MessageHandler(filters.Document.FileExtension("zip"), handle_zip_upload))
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_login_process, pattern="^user_login$")],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_otp)],
            OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, verify_otp_and_check_name),
                CallbackQueryHandler(resend_otp_callback, pattern="^resend_otp$"),
            ],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name_and_continue)]
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(cancel_process_callback, pattern="^cancel_action$")]
    )
    application.add_handler(conv_handler)
    
    application.add_handler(CallbackQueryHandler(core_callback, pattern="^admin_|^main_menu$|^admin_panel$|^finish_link_creation$|^get_my_links$"))
    application.add_handler(MessageHandler(filters.TEXT | filters.Document.FileExtension("txt"), handle_admin_text_document))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logging.info("System initialized successfully.")
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass

