#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import shutil
import sqlite3
import threading
import logging
import ipaddress
import requests
import telebot
import schedule

from telebot import types
from telebot.apihelper import ApiTelegramException
from dotenv import load_dotenv
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ======================================
# LOAD ENV
# ======================================

load_dotenv()

BOT_TOKEN = os.getenv("8887374895:AAGPDAmFEWRfrw8dOq3IbJ2c9DJDM49YqTQ")
ADMIN_ID = int(os.getenv("8210146346", "0"))
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@saniedit9")
DATABASE_PATH = "ip_tracker.db"

INITIAL_BALANCE = 5
IP_CHECK_COST = 1

FLOOD_MAX = 5
FLOOD_TIME = 10
FLOOD_BAN = 60

# ======================================
# VALIDATION
# ======================================

if not BOT_TOKEN:
    print("BOT TOKEN MISSING")
    exit()

if ADMIN_ID == 0:
    print("ADMIN ID MISSING")
    exit()

# ======================================
# LOGGING
# ======================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# ======================================
# BOT
# ======================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode='Markdown',
    threaded=True,
    num_threads=20
)

# ======================================
# REQUEST SESSION
# ======================================

session = requests.Session()

retry = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504]
)

adapter = HTTPAdapter(max_retries=retry)

session.mount('http://', adapter)
session.mount('https://', adapter)

# ======================================
# DATABASE
# ======================================

_local = threading.local()


def get_conn():

    if not hasattr(_local, 'conn'):

        _local.conn = sqlite3.connect(
            DATABASE_PATH,
            check_same_thread=False,
            timeout=30
        )

        _local.conn.row_factory = sqlite3.Row

        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _local.conn.execute("PRAGMA temp_store=MEMORY")

    return _local.conn


def init_db():

    conn = get_conn()

    conn.executescript('''

    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance INTEGER DEFAULT 5,
        total_checks INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    ''')

    conn.commit()

# ======================================
# FLOOD SYSTEM
# ======================================

flood_data = defaultdict(list)
banned_users = {}
lock = threading.Lock()


def is_flood(user_id):

    now = time.time()

    with lock:

        if user_id in banned_users:

            if now < banned_users[user_id]:
                return True

            del banned_users[user_id]

        flood_data[user_id] = [
            t for t in flood_data[user_id]
            if now - t < FLOOD_TIME
        ]

        flood_data[user_id].append(now)

        if len(flood_data[user_id]) > FLOOD_MAX:

            banned_users[user_id] = now + FLOOD_BAN
            flood_data[user_id] = []

            return True

    return False

# ======================================
# USER SYSTEM
# ======================================


def register_user(user_id, username=''):

    conn = get_conn()

    row = conn.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row:
        return

    conn.execute(
        "INSERT INTO users(user_id, username, balance) VALUES(?,?,?)",
        (user_id, username, INITIAL_BALANCE)
    )

    conn.commit()


def get_user(user_id):

    conn = get_conn()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row:
        return dict(row)

    return None


def deduct_balance(user_id, amount):

    conn = get_conn()

    conn.execute(
        "UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?",
        (amount, user_id, amount)
    )

    conn.commit()

    return conn.execute("SELECT changes()").fetchone()[0] > 0


def refund_balance(user_id, amount):

    conn = get_conn()

    conn.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id=?",
        (amount, user_id)
    )

    conn.commit()

# ======================================
# SAFE MESSAGE
# ======================================


def safe_send(chat_id, text, **kwargs):

    try:
        return bot.send_message(chat_id, text, **kwargs)

    except ApiTelegramException as e:
        logging.error(e)

    except Exception as e:
        logging.error(e)


def safe_edit(chat_id, message_id, text, **kwargs):

    try:

        bot.edit_message_text(
            text,
            chat_id,
            message_id,
            **kwargs
        )

    except ApiTelegramException as e:

        if 'message is not modified' in str(e):
            return

        logging.error(e)

    except Exception as e:
        logging.error(e)

# ======================================
# CHANNEL CHECK
# ======================================


def check_channel(user_id):

    try:

        member = bot.get_chat_member(
            REQUIRED_CHANNEL,
            user_id
        )

        return member.status in [
            'member',
            'administrator',
            'creator'
        ]

    except Exception as e:

        logging.error(e)

        return False

# ======================================
# VALIDATE IP
# ======================================


def is_valid_ip(ip):

    try:
        ipaddress.ip_address(ip)
        return True

    except ValueError:
        return False

# ======================================
# KEYBOARD
# ======================================


def menu_keyboard():

    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        types.InlineKeyboardButton(
            '🔍 Lookup IP',
            callback_data='lookup'
        ),

        types.InlineKeyboardButton(
            '💰 Balance',
            callback_data='balance'
        )
    )

    kb.add(
        types.InlineKeyboardButton(
            '📢 Join Channel',
            url=f'https://t.me/{REQUIRED_CHANNEL.replace("@", "")}'
        )
    )

    return kb

# ======================================
# START
# ======================================


@bot.message_handler(commands=['start'])
def start_command(message):

    user_id = message.from_user.id

    if is_flood(user_id):
        return

    register_user(
        user_id,
        message.from_user.username or ''
    )

    if not check_channel(user_id):

        safe_send(
            user_id,
            '📢 Join Required Channel First'
        )

        return

    user = get_user(user_id)

    safe_send(
        user_id,
        f"🌐 *IP Tracker Bot*\n\n"
        f"💰 Balance: *{user['balance']}*\n"
        f"🔍 Total Checks: *{user['total_checks']}*",
        reply_markup=menu_keyboard()
    )

# ======================================
# LOOKUP
# ======================================


@bot.message_handler(commands=['ip'])
def ip_command(message):

    user_id = message.from_user.id

    if is_flood(user_id):
        return

    if not check_channel(user_id):
        return

    args = message.text.split()

    if len(args) < 2:

        safe_send(
            user_id,
            'Usage: /ip 8.8.8.8'
        )

        return

    lookup_ip(user_id, args[1])

# ======================================
# LOOKUP FUNCTION
# ======================================


def lookup_ip(user_id, ip):

    if not is_valid_ip(ip):

        safe_send(
            user_id,
            '❌ Invalid IP Address'
        )

        return

    ok = deduct_balance(user_id, IP_CHECK_COST)

    if not ok:

        safe_send(
            user_id,
            '❌ Low Balance'
        )

        return

    loading = safe_send(
        user_id,
        '🔍 Searching...'
    )

    try:

        response = session.get(
            f'https://ipwho.is/{ip}',
            timeout=(5, 10),
            headers={
                'User-Agent': 'Mozilla/5.0'
            },
            verify=True
        )

        data = response.json()

    except Exception as e:

        refund_balance(user_id, IP_CHECK_COST)

        logging.error(e)

        safe_edit(
            user_id,
            loading.message_id,
            '❌ API Failed'
        )

        return

    if not data.get('success'):

        refund_balance(user_id, IP_CHECK_COST)

        safe_edit(
            user_id,
            loading.message_id,
            '❌ Lookup Failed'
        )

        return

    conn = get_conn()

    conn.execute(
        'UPDATE users SET total_checks = total_checks + 1 WHERE user_id=?',
        (user_id,)
    )

    conn.commit()

    user = get_user(user_id)

    result = (
        f"🌐 *IP RESULT*\n"
        f"━━━━━━━━━━━━━━\n"
        f"📌 IP: `{data.get('ip', 'N/A')}`\n"
        f"🏳️ Country: `{data.get('country', 'N/A')}`\n"
        f"🏙 City: `{data.get('city', 'N/A')}`\n"
        f"📡 ISP: `{data.get('connection', {}).get('isp', 'N/A')}`\n"
        f"🕒 Timezone: `{data.get('timezone', {}).get('id', 'N/A')}`\n"
        f"📍 Latitude: `{data.get('latitude', 'N/A')}`\n"
        f"📍 Longitude: `{data.get('longitude', 'N/A')}`\n\n"
        f"💰 Balance: *{user['balance']}*"
    )

    safe_edit(
        user_id,
        loading.message_id,
        result
    )

# ======================================
# CALLBACKS
# ======================================


@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):

    user_id = call.from_user.id

    try:

        if call.data == 'balance':

            user = get_user(user_id)

            safe_send(
                user_id,
                f"💰 Balance: *{user['balance']}*"
            )

        elif call.data == 'lookup':

            safe_send(
                user_id,
                'Use Command: /ip 8.8.8.8'
            )

        bot.answer_callback_query(call.id)

    except Exception as e:

        logging.error(e)

# ======================================
# DATABASE BACKUP
# ======================================


def backup_database():

    try:

        shutil.copyfile(
            DATABASE_PATH,
            'backup_ip_tracker.db'
        )

        logging.info('DATABASE BACKUP DONE')

    except Exception as e:

        logging.error(e)

# ======================================
# SCHEDULER
# ======================================


def scheduler_loop():

    schedule.every().day.at('03:00').do(
        backup_database
    )

    while True:

        try:
            schedule.run_pending()

        except Exception as e:
            logging.error(e)

        time.sleep(30)

# ======================================
# MAIN
# ======================================


if __name__ == '__main__':

    logging.info('BOT STARTING')

    init_db()

    threading.Thread(
        target=scheduler_loop,
        daemon=True
    ).start()

    while True:

        try:

            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True
            )

        except Exception as e:

            logging.error(e)

            time.sleep(5)