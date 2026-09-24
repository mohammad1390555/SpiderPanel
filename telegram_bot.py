"""
SpiderPanel Telegram Bot ─ پنل فروش و مدیریت اشتراک از داخل تلگرام
================================================================
Long-polling bot implemented with httpx (already a panel dependency).
Wired into main.py via start_bot()/stop_bot()/restart_bot().

User experience mirrors the PanahNet worker bot (user.js):
  * Main menu (🕹) with inline buttons: فروشگاه / خرید، سرویس‌های من، کیف
    پول، تست رایگان، حساب کاربری، پشتیبانی.
  * Wallet (کیف پول): شارژ کارت‌به‌کارت + رسید → صف تأیید ادمین → شارژ
    موجودی → خرید پلن از روی موجودی.
  * کد هدیه (gift code) ساخته‌شده توسط ادمین برای حجم/مدت رایگان.
  * تست رایگان یک‌باره.
  * معرفی: کد اختصاصی + پاداش درصدی از اولین خرید دوست روی کیف پول.
  * لینک سابسکریپشن Worker-استایل: https://host/sub/{hash} در قالب‌های
    base64 / clash / singbox / vjson.

Admin (telegram ids در SETTINGS["telegram_bot"]["admin_ids"]):
  آمار، کاربران، ساخت/مدیریت، پیام همگانی، تأیید رسیدها (/approve يا دکمه),
  ساخت کد هدیه (/gift), مدیریت پلن‌ها (/plans, /plan add ...), تنظیم کارت
  (/setcard), تنظیمات ربات.

Config (SETTINGS["telegram_bot"]):
    enabled, token, admin_ids, channel, welcome_msg, rules_text, support_text,
    card_number, card_owner, min_charge, max_charge, plans[], trial_gb,
    trial_days, referral_percent, gift_codes{}, charges{}, wallets{},
    refs{}, _trial_used[], txns{}
"""

import asyncio
import json
import logging
import os
import re
import io
import secrets
import time

import main as P

logger = # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # logging.getLogger("Panel-Bot")

TG_API = "https://api.telegram.org/bot{token}/{method}"

POLL_TASK: asyncio.Task | None = None
_stopped = False
_offset = 0

# chat_id -> {"step": "...", "data": {...}}  for multi-step flows
BOT_STATE: dict[int, dict] = {}
# callback registry used to re-render the admin user list
LIST_PAGES: dict[int, dict] = {}
_BOT_USERNAME = ""

ADMIN_MENU = [
    ["📈 آمار", "➕ کاربر جدید"],
    ["👥 لیست کاربران", "📢 پیام همگانی"],
    ["🕹 منو", "⚙️ تنظیمات ربات"],
]

USER_MENU = [
    ["🕹 منو", "🛒 خرید"],
    ["💳 کیف پول", "🎁 کد هدیه"],
    ["🛟 پشتیبانی"],
]

_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# ── Config helpers ──────────────────────────────────────────────────────────
def _cfg() -> dict:
    b = dict(P.SETTINGS.get("telegram_bot") or {})
    if not b.get("token"):
        b["token"] = str(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    admins = b.get("admin_ids") or []
    if not admins:
        env_admins = os.environ.get("TELEGRAM_BOT_ADMIN_IDS") or ""
        admins = [int(x) for x in env_admins.split(",") if str(x).strip().lstrip("-").isdigit()]
    b["admin_ids"] = admins
    if not b.get("welcome_msg"):
        b["welcome_msg"] = (
            "🤖 <b>ربات مدیریت اشتراک</b>\n\n"
            "به ربات اختصاصی پنل خوش آمدید ✨\n"
            "از دکمه‌های زیر اشتراک خود را مدیریت کنید."
        )
    b.setdefault("rules_text", (
        "📜 قوانین استفاده از سرویس\n\n"
        "۱) فروش و واگذاری اشتراک به دیگران ممنوع است و باعث حذف حساب می‌شود.\n"
        "۲) استفاده از ترافیک برای فعالیت‌های غیرقانونی ممنوع است.\n"
        "۳) با ادامه استفاده، این قوانین را می‌پذیرید."
    ))
    b.setdefault("support_text", "")
    b.setdefault("card_number", "")
    b.setdefault("card_owner", "")
    b.setdefault("min_charge", 50000)
    b.setdefault("max_charge", 20000000)
    b.setdefault("plans", [
        {"id": "p1", "name": "شروع", "gb": 20, "days": 30, "price": 99000},
        {"id": "p2", "name": "محبوب", "gb": 60, "days": 30, "price": 199000},
        {"id": "p3", "name": "طولانی", "gb": 60, "days": 90, "price": 399000},
    ])
    b.setdefault("trial_gb", 5)
    b.setdefault("trial_days", 3)
    b.setdefault("referral_percent", 20)
    b.setdefault("gift_codes", {})
    b.setdefault("charges", {})
    b.setdefault("wallets", {})
    b.setdefault("refs", {})
    b.setdefault("txns", {})
    b.setdefault("_trial_used", [])
    return b


def _token() -> str:
    return _cfg()["token"]


def _is_admin(tg_id) -> bool:
    try:
        return int(tg_id) in _cfg()["admin_ids"]
    except Exception:
        return False


async def _save_cfg(**changes):
    """Persist bot settings inside SETTINGS and trigger a state save."""
    async with P.SETTINGS_LOCK:
        b = P.SETTINGS.setdefault("telegram_bot", {})
        for k, v in changes.items():
            b[k] = v
    asyncio.create_task(P.save_state())


def _toman(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _fa_digits(s: str) -> str:
    return str(s or "").translate(_FA_DIGITS)


def _parse_money(s: str):
    s = _fa_digits(str(s or "").strip())
    s = s.replace(",", "").replace("٬", "").replace(" ", "")
    s = s.replace("تومان", "").replace("toman", "").replace("هزار", "000").replace("هزارتومان", "000")
    if not re.fullmatch(r"\d+", s):
            try:
        return int(s)
    except Exception:
        

def _parse_int(s: str):
    s = _fa_digits(str(s or "").strip()).replace(",", "")
    if not re.fullmatch(r"\d+", s):
            try:
        return int(s)
    except Exception:
        

# ── Telegram API helpers ────────────────────────────────────────────────────
async def _call(token: str, method: str, json_body=None, files=None, data=None, timeout: float = 30.0) -> dict:
    url = TG_API.format(token=token, method=method)
    try:
        resp = await P.http_client.post(url, json=json_body, data=data, files=files, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "description": f"network error: {exc}"[:200]}
    try:
        payload = resp.json()
    except Exception:
        payload = {"ok": False, "description": resp.text[:300]}
    return payload if isinstance(payload, dict) else {"ok": False, "description": str(payload)[:300]}


async def _send(chat_id, text, buttons=None, parse="HTML"):
    body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse:
        body["parse_mode"] = parse
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    return await _call(_token(), "sendMessage", json_body=body)


async def _send_menu(chat_id, text, rows, parse="HTML"):
    body = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse,
        "disable_web_page_preview": True,
        "reply_markup": {"keyboard": rows, "resize_keyboard": True, "is_persistent": True},
    }
    return await _call(_token(), "sendMessage", json_body=body)


async def _edit(chat_id, msg_id, text, buttons=None, parse="HTML"):
    body = {"chat_id": chat_id, "message_id": msg_id, "text": text, "disable_web_page_preview": True}
    if parse:
        body["parse_mode"] = parse
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    return await _call(_token(), "editMessageText", json_body=body)


async def _del_menu(chat_id, msg_id):
    await _call(_token(), "deleteMessage", json_body={"chat_id": chat_id, "message_id": msg_id})


async def _send_photo(chat_id, png_bytes, caption=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
    files = {"photo": ("config_qr.png", png_bytes, "image/png")}
    return await _call(_token(), "sendPhoto", data=data, files=files)


async def _forward_photo(chat_id, file_id, caption=None, buttons=None):
    data = {"chat_id": chat_id, "photo": file_id}
    if caption:
        data["caption"] = caption[:1024]
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return await _call(_token(), "sendPhoto", data=data)


async def _answer_cb(cb_id, text=None, alert=False):
    body = {"callback_query_id": cb_id}
    if text:
        body["text"] = text
    if alert:
        body["show_alert"] = True
    await _call(_token(), "answerCallbackQuery", json_body=body)


async def _bot_username() -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    r = await _call(_token(), "getMe")
    if r.get("ok"):
        _BOT_USERNAME = str((r.get("result") or {}).get("username") or "")
    return _BOT_USERNAME


def _html(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Panel helpers ───────────────────────────────────────────────────────────
def _pick_inbounds() -> list:
    """Choose the best inbounds for a bot-created user:
    worker inbound (if the worker is connected) then the default TLS+WS relay."""
    ids: list = []
    wid = next((i for i, ib in P.INBOUNDS.items() if (ib.get("protocol") or "").lower() == "worker"), None)
    if wid and P.WORKER.get("connected") is True:
        ids.append(wid)
    dws = P.find_default_tls_ws_inbound_id()
    if dws and dws not in ids:
        ids.append(dws)
    if not ids:
        for i, ib in P.INBOUNDS.items():
            if ((ib.get("protocol") or "").lower() == "vless"
                    and (ib.get("network") or "").lower() == "ws"
                    and (ib.get("security") or "").lower() == "tls"):
                ids.append(i)
                break
        if not ids:
            first = next(iter(P.INBOUNDS), None)
            if first:
                ids.append(first)
    return ids


async def _bound_user(chat_id):
    """Return (user_id, user) for the telegram chat, if one exists."""
    tid = str(chat_id)
    async with P.USERS_LOCK:
        for uid, u in P.USERS.items():
            if str(u.get("telegram_id") or "") == tid:
                return uid, dict(u)
    return None, None


async def _find_user_by_ref(ref: str):
    """Resolve a username or user_id to (user_id, user)."""
    ref = str(ref or "").strip().lower()
    if not ref:
        return None, None
    async with P.USERS_LOCK:
        if ref in P.USERS:
            return ref, dict(P.USERS[ref])
        for uid, u in P.USERS.items():
            if str(u.get("username") or "").strip().lower() == ref:
                return uid, dict(u)
            if str(u.get("config_uuid") or "").lower() == ref:
                return uid, dict(u)
    return None, None


def _status_icon(status: str, is_active: bool = True, used=0, limit=0) -> str:
    s = str(status or "active").lower()
    if s == "disabled":
        return "متوقف ⏸"
    if s == "expired":
        return "منقضی ⛔"
    if not is_active:
        if limit > 0 and used >= limit:
            return "حجم تمام شده"
        return "غیرفعال"
    return "فعال ✅"


def _usage_bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = round(width * pct / 100)
    return "▓" * filled + "░" * (width - filled)


async def _sub_report(user: dict) -> dict:
    """Build the worker-style subscription report for a user."""
    cuuid = user.get("config_uuid") or ""
    try:
        sub_url = P.get_bot_sub_link(cuuid) or P.sub_hash_url(cuuid) or ""
    except Exception:
        sub_url = P.sub_hash_url(cuuid) or ""
    data = await P._build_subscription_data_by_uuid(cuuid)
    configs = data.get("configs") or []
    vless = data.get("vless_link") or data.get("config") or (configs[0] if configs else "")
    worker_lines = [c for c in configs if "/ws/" in c and c.startswith("vless://")]
    return {
        "username": data.get("username") or user.get("username") or cuuid,
        "status": data.get("status") or "active",
        "is_active": bool(data.get("is_active")),
        "used": data.get("traffic_used_bytes") or 0,
        "used_fmt": data.get("traffic_used_fmt") or "0 B",
        "limit": data.get("traffic_limit_bytes") or 0,
        "limit_fmt": data.get("traffic_limit_fmt") or "∞",
        "pct": data.get("traffic_percent") or 0,
        "expire_at": data.get("expire_at"),
        "expire_days": data.get("expire_days"),
        "created_at": data.get("created_at"),
        "sub_url": sub_url,
        "hash": (sub_url.rsplit("/", 1)[-1] if "/sub/" in sub_url else ""),
        "vless": vless,
        "worker_count": len(worker_lines),
        "configs": configs,
        "max_ips": data.get("max_ip_per_user") or 0,
        "used_ips": data.get("used_ips") or 0,
    }


async def _extend_user(chat_id, tg_user, add_gb: float, add_days: int, note: str = ""):
    """Extend the bound user's traffic/expiry, creating the account if needed."""
    uid, user = await _bound_user(chat_id)
    created = False
    if not uid:
        username = str(tg_user.get("username") or "").strip().lower() or f"tg-{chat_id}"
        uid, user, created = await _create_sub_user(chat_id, tg_user, username)

    async with P.USERS_LOCK:
        u = P.USERS.get(uid)
        if not u:
            return None, created
        add_bytes = int(float(add_gb) * 1024 ** 3) if add_gb > 0 else 0
        cur = int(u.get("traffic_limit_bytes") or 0)
        u["traffic_limit_bytes"] = cur + add_bytes
        now = P.datetime.now()
        exp = u.get("expire_at")
        try:
            base = P.datetime.fromisoformat(exp) if exp else now
        except Exception:
            base = now
        if base < now:
            base = now
        if add_days > 0:
            u["expire_at"] = (base + P.timedelta(days=add_days)).isoformat()
            if str(u.get("status") or "active") in ("expired", "disabled"):
                u["status"] = "active"
        uu = dict(u)
        cuuid = u.get("config_uuid")

    if cuuid:
        async with P.LINKS_LOCK:
            if cuuid in P.LINKS:
                P.LINKS[cuuid]["limit_bytes"] = uu.get("traffic_limit_bytes", 0)
                P.LINKS[cuuid]["expires_at"] = uu.get("expire_at")
                P.LINKS[cuuid]["active"] = True
    await P.save_state()
    if P.WORKER.get("connected") is True and (cuuid in P.LINKS and P._user_uses_worker_inbound(uu)):
        asyncio.create_task(P._worker_sync_users())
    if note:
        P.log_activity("user", f"{note} برای «{uu.get('username')}» از ربات", "ok")
    return uu, created


async def _wallet_balance(tg_id) -> int:
    cfg = _cfg()
    return int(cfg["wallets"].get(str(tg_id), 0) or 0)


async def _wallet_add(tg_id, amount, note=""):
    cfg = _cfg()
    wid = str(tg_id)
    wallets = dict(cfg["wallets"])
    wallets[wid] = int(wallets.get(wid, 0) or 0) + int(amount)
    txns = dict(cfg["txns"])
    tx = txns.get(wid) or []
    tx.append({"type": "deposit", "amount": int(amount), "note": note, "ts": time.time()})
    txns[wid] = tx[-30:]
    await _save_cfg(wallets=wallets, txns=txns, charges=cfg["charges"])


async def _wallet_spend(tg_id, amount, note="") -> bool:
    cfg = _cfg()
    wid = str(tg_id)
    wallets = dict(cfg["wallets"])
    bal = int(wallets.get(wid, 0) or 0)
    if bal < int(amount):
        return False
    wallets[wid] = bal - int(amount)
    txns = dict(cfg["txns"])
    tx = txns.get(wid) or []
    tx.append({"type": "purchase", "amount": -int(amount), "note": note, "ts": time.time()})
    txns[wid] = tx[-30:]
    await _save_cfg(wallets=wallets, txns=txns)
    return True


def _ref_record(tg_id) -> dict:
    cfg = _cfg()
    rec = cfg["refs"].get(str(tg_id))
    if not rec:
        rec = {"code": secrets.token_urlsafe(4)[:6], "invited_by": None, "count": 0, "paid": False}
    else:
        rec = dict(rec)
    return rec


async def _save_ref_record(tg_id, rec):
    cfg = _cfg()
    refs = dict(cfg["refs"])
    refs[str(tg_id)] = rec
    await _save_cfg(refs=refs)


async def _ref_owner_by_code(code: str):
    cfg = _cfg()
    code = str(code or "").strip().lower()
    for tid, rec in (cfg.get("refs") or {}).items():
        if str(rec.get("code") or "").lower() == code:
            return str(tid)
    

# ── User creation ───────────────────────────────────────────────────────────
async def _create_sub_user(chat_id: int, tg_user: dict, username: str, limit_gb: float = 0.0, expire_days: int = 0):
    """Create a panel user + link bound to this telegram account.

    Returns (user_id, user, created). Never creates a duplicate for the
    same telegram account.
    """
    existing_uid, existing = await _bound_user(chat_id)
    if existing_uid:
        return existing_uid, existing, False

    limit_gb = max(0.0, float(limit_gb or 0))
    expire_days = max(0, int(expire_days or 0))
    username = str(username or "").strip()[:40] or f"tg-{chat_id}"

    async with P.USERS_LOCK:
        taken = {str(u.get("username") or "").strip() for u in P.USERS.values()}
        if username in taken:
            base = username[:36]
            n = 1
            while f"{base}{n}" in taken:
                n += 1
            username = f"{base}{n}"

        user_id = P.generate_short_id()
        config_uuid = P.generate_uuid()
        subscription_uuid = P.secrets.token_urlsafe(16)
        traffic_limit_bytes = int(float(limit_gb) * 1024 ** 3) if limit_gb > 0 else 0
        expire_at = (P.datetime.now() + P.timedelta(days=expire_days)).isoformat() if expire_days > 0 else None
        inbound_ids = _pick_inbounds()
        inbound_id = inbound_ids[0] if inbound_ids else None
        path = f"/ws/{config_uuid}"

        user = {
            "username": username,
            "password_hash": P.hash_password(P.secrets.token_urlsafe(12)),
            "protocol": "vless",
            "traffic_limit_bytes": traffic_limit_bytes,
            "traffic_used_bytes": 0,
            "expire_at": expire_at,
            "concurrent_connections": 0,
            "created_at": P.datetime.now().isoformat(),
            "status": "active",
            "server": "telegram-bot",
            "config_uuid": config_uuid,
            "subscription_uuid": subscription_uuid,
            "sni": "",
            "proxy_ip": "",
            "proxy_ips": [],
            "proxy_ip_enabled": False,
            "custom_ip_type": "",
            "custom_ip_inbounds": {"cf": [], "railway": []},
            "sni_spoof_v2box": False,
            "inbound_id": inbound_id,
            "inbound_ids": inbound_ids,
            "path": path,
            "transport_type": "ws",
            "telegram_id": str(chat_id),
            "tg_username": str(tg_user.get("username") or ""),
            "tg_first_name": str(tg_user.get("first_name") or ""),
            "created_by": "telegram_bot",
            "node_sync_password": P.secrets.token_urlsafe(18),
            "node_configs": {},
            "node_sync_state": {},
            "node_traffic": {},
            "node_traffic_used_bytes": 0,
        }
        P.USERS[user_id] = user
        _user_record = dict(user)

    relay_default_id = P.find_default_tls_ws_inbound_id()
    relay_enabled = bool(relay_default_id and relay_default_id in inbound_ids)

    async with P.LINKS_LOCK:
        P.LINKS[config_uuid] = {
            "label": username,
            "limit_bytes": traffic_limit_bytes,
            "used_bytes": 0,
            "created_at": _user_record["created_at"],
            "active": True,
            "expires_at": expire_at,
            "note": f"لینک تلگرام {username}",
            "is_default": False,
            "sub_id": None,
            "protocol": "vless-ws",
            "transport_type": "ws",
            "xhttp_settings": {},
            "path": path,
            "user_id": user_id,
            "inbound_id": inbound_id,
            "relay_enabled": relay_enabled,
            "relay_inbound_id": relay_default_id if relay_enabled else None,
        }
        P.PATH_INDEX[config_uuid] = config_uuid
        P.PATH_INDEX[path.lstrip("/")] = config_uuid
    # Pre-create the worker-style sub hash so the first /sub/{hash} resolves.
    try:
        P.ensure_sub_hash(config_uuid)
    # FIXME: [auto-fix]: handle exception

    await P.save_state()
    if any((P.INBOUNDS.get(iid) or {}).get("protocol") == "worker" for iid in inbound_ids) and P.WORKER.get("connected") is True:
        try:
            await P._worker_sync_users()
        except Exception as exc:
            logger.warning("worker sync after bot user failed: %s", exc)
    try:
        asyncio.create_task(P._xray_apply())
    # FIXME: [auto-fix]: handle exception
    P.log_activity("user", f"کاربر «{username}» از طریق ربات تلگرام ساخته شد", "ok")
    return user_id, dict(P.USERS.get(user_id) or _user_record), True


def _qrcode_png(text: str) -> bytes:
    import qrcode
    qr = qrcode.QRCode(version=1, box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Formatting (worker-style) ───────────────────────────────────────────────
def _fmt_gb(n: int):
    try:
        return f"{int(n) / 1024 ** 3:.2f}".rstrip("0").rstrip(".") + " GB"
    except Exception:
        return "∞"


def _fmt_remain_days(expire_at) -> str:
    if not expire_at:
        return "نامحدود ♾"
    try:
        exp = P.datetime.fromisoformat(str(expire_at))
    except Exception:
        return "—"
    remain = exp - P.datetime.now()
    if remain.total_seconds() <= 0:
        return "منقضی شده"
    days = remain.days
    hours = remain.seconds // 3600
    mins = (remain.seconds % 3600) // 60
    if days > 0:
        return f"{days} روز"
    if hours > 0:
        return f"{hours} ساعت"
    return f"{mins} دقیقه"


def _fmt_service_card(r: dict) -> str:
    pct = float(r["pct"] or 0)
    lines = [
        "📋 <b>سرویس‌های شما</b>\n",
        f"👤 نام: <code>{_html(r['username'])}</code>",
        f"📶 وضعیت: {_status_icon(r['status'], r['is_active'], r['used'], r['limit'])}",
        "",
        "🚀 مصرف",
        f"<code>{_usage_bar(pct)}</code> {pct:.1f}%",
        f"  {_html(r['used_fmt'])} از {_html(r['limit_fmt'])}" + ("" if r['limit'] else " (نامحدود)") ,
        "",
        "⏳ زمان",
        f"  انقضا: <b>{_fmt_remain_days(r['expire_at'])}</b>",
        "",
        "🔗 <b>لینک اختصاصی اشتراک</b> — لمس کن و کپی کن👇",
        f"<code>{_html(r['sub_url'])}</code>",
    ]
    return "\n".join(lines)


async def _service_menu(chat_id) -> tuple:
    """Return (text, buttons) for the services view of the bound user."""
    uid, user = await _bound_user(chat_id)
    if not uid:
        return ("⚠️ هنوز سرویسی ندارید.\nاز فروشگاه یک پلن بخرید یا تست رایگان بگیرید.",
                [[{"text": "🛒 خرید", "callback_data": "u:buy"}, {"text": "🎁 تست رایگان", "callback_data": "u:trial"}]])
    try:
        r = await _sub_report(user)
    except Exception:
        return ("⛔ خواندن وضعیت سرویس ممکن نشد.",
                [[{"text": "🏠 منوی اصلی", "callback_data": "u:home"}]])
    btns = [
        [{"text": "🔗 لینک‌های ساب", "callback_data": "u:links"},
         {"text": "⏳ تمدید", "callback_data": "u:buy"}],
        [{"text": "⏸ متوقف/ادامه", "callback_data": "u:toggle"},
         {"text": "🗑 حذف", "callback_data": "u:del"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "u:home"}],
    ]
    return _fmt_service_card(r), btns


def _u_home_kb(cfg: dict) -> list:
    return [
        [{"text": "🛒 فروشگاه", "callback_data": "u:buy"},
         {"text": "📋 سرویس‌های من", "callback_data": "u:services"}],
        [{"text": "💳 کیف پول", "callback_data": "u:wallet"},
         {"text": "🎁 تست رایگان", "callback_data": "u:trial"}],
        [{"text": "👤 حساب کاربری", "callback_data": "u:account"}],
        [{"text": "🛟 پشتیبانی", "callback_data": "u:support"}],
    ]


# ── Menu actions (worker-style `u:*`) ───────────────────────────────────────
async def _menu_home(chat_id):
    cfg = _cfg()
    await _send(chat_id,
                "🕹 <b>منوی اصلی</b>\n\nاز دکمه‌های زیر انتخاب کنید 👇",
                buttons=_u_home_kb(cfg))


async def _menu_buy(chat_id, msg_id=None):
    cfg = _cfg()
    plans = cfg.get("plans") or []
    if not plans:
        txt = "⛔ فروشگاه در حال حاضر پلنی ندارد."
        btns = [[{"text": "🏠 منوی اصلی", "callback_data": "u:home"}]]
    else:
        txt = "🛍 <b>فروشگاه</b>\n\nپلن مورد نظرت را انتخاب کن:\n"
        for p in plans:
            gb_i = int(p.get("gb") or 0)
            txt += f"\n▫️ <b>{_html(p.get('name'))}</b>\n   {gb_i} GB · {p.get('days')} روز · 💰 {_toman(p.get('price'))} تومان"
        btns = [[{"text": f"{p.get('name')} · {p.get('gb')} GB · {_toman(p.get('price'))}",
                  "callback_data": f"u:pkg:{p.get('id')}"}] for p in plans]
        btns.append([{"text": "🏠 منوی اصلی", "callback_data": "u:home"}])
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _menu_confirm_buy(chat_id, msg_id, plan):
    bal = await _wallet_balance(chat_id)
    txt = (
        f"🧾 <b>تأیید خرید</b>\n\n"
        f"📦 پلن: <b>{_html(plan.get('name'))}</b>\n"
        f"⚡ حجم: {plan.get('gb')} GB\n"
        f"⏳ مدت: {plan.get('days')} روز\n"
        f"💰 قیمت: <b>{_toman(plan.get('price'))} تومان</b>\n\n"
        f"💳 موجودی کیف پول: <b>{_toman(bal)} تومان</b>"
    )
    if bal >= int(plan.get("price") or 0):
        txt += "\n\n✅ آماده پرداخت هستید."
        btns = [[{"text": "✅ تأیید و پرداخت", "callback_data": f"u:conf:{plan.get('id')}"}],
                [{"text": "🔙 فروشگاه", "callback_data": "u:buy"}]]
    else:
        need = int(plan.get("price") or 0) - bal
        txt += f"\n\n⚠️ موجودی کافی نیست.\nکمبود: <b>{_toman(need)} تومان</b>"
        btns = [[{"text": "💳 شارژ کیف پول", "callback_data": "u:charge"}],
                [{"text": "🔙 فروشگاه", "callback_data": "u:buy"}]]
    await _edit(chat_id, msg_id, txt, buttons=btns)


async def _exec_purchase(chat_id, tg_user, plan):
    price = int(plan.get("price") or 0)
    ok = await _wallet_spend(chat_id, price, f"خرید پلن {plan.get('name')}")
    if not ok:
        await _send(chat_id, "⚠️ موجودی کیف پول کافی نیست.", buttons=[[{"text": "💳 شارژ", "callback_data": "u:charge"}]])
        return
    u, created = await _extend_user(chat_id, tg_user, plan.get("gb") or 0, plan.get("days") or 0, f"خرید پلن {plan.get('name')}")
    # referral reward: inviter gets % of first paid purchase
    rec = _ref_record(chat_id)
    if rec.get("invited_by") and not rec.get("paid"):
        rec["paid"] = True
        await _save_ref_record(chat_id, rec)
        cfg = _cfg()
        bonus = round(price * int(cfg.get("referral_percent") or 0) / 100)
        if bonus > 0:
            await _wallet_add(rec["invited_by"], bonus, "پاداش معرفی (خرید اول دعوت‌شده)")
            inv_rec = _ref_record(rec["invited_by"])
            inv_rec["count"] = int(inv_rec.get("count") or 0) + 1
            await _save_ref_record(rec["invited_by"], inv_rec)
            try:
                await _send(int(rec["invited_by"]),
                            f"🎉 <b>پاداش معرفی!</b>\n\nبه کیف پولت <b>{_toman(bonus)} تومان</b> اضافه شد.\n"
                            f"دعوت‌ت با موفقیت اولین خرید انجام داد. 🚀",
                            buttons=[[{"text": "💳 کیف پول", "callback_data": "u:wallet"}]])
            # FIXME: [auto-fix]: handle exception
    if u:
        try:
            r = await _sub_report(u)
            link = r["sub_url"]
        except Exception:
            link = ""
        txt = (
            f"✅ <b>خرید موفق بود — خوش آمدی به شبکه! 🚀</b>\n\n"
            f"سرویس <b>{_html(plan.get('name'))}</b> الان روی حسابت فعال است.\n\n"
            f"─ ─ ─ ─ ─ ─ ─ ─\n"
            f"🔗 <b>لینک اختصاصی اشتراک</b>\n"
            f"لمس کن و کپی کن، بعد در Hiddify یا v2rayNG از کلیپ‌بورد وارد کن:\n\n"
            f"<code>{_html(link)}</code>"
        )
        btns = [[{"text": "📋 سرویس‌های من", "callback_data": "u:services"},
                 {"text": "🏠 منو", "callback_data": "u:home"}]]
        await _send(chat_id, txt, buttons=btns)
        try:
            if r and r.get("vless"):
                await _send_photo(chat_id, _qrcode_png(r["vless"]), caption=f"QR کانفیگ {_html(r['username'])[:40]}")
        # FIXME: [auto-fix]: handle exception


async def _menu_wallet(chat_id, msg_id=None):
    bal = await _wallet_balance(chat_id)
    cfg = _cfg()
    txt = (
        "💳 <b>کیف پول</b>\n"
        "صندوق شخصی تو برای خرید و تمدید\n\n"
        f"─ ─ ─ ─ ─ ─ ─ ─\n"
        f"💰 موجودی قابل استفاده: <b>{_toman(bal)} تومان</b>\n"
        f"🧾 تعداد تراکنش‌ها: <b>{len((cfg.get('txns') or {}).get(str(chat_id), []))}</b>\n\n"
        "با شارژ کارت‌به‌کارت موجودی همواره می‌ماند تا پلن بخری یا سرویس را تمدید کنی — لازم نیست هر بار رسید جدا بفرستی."
        " اگر موجودی کم است، «➕ شارژ» را بزن، مبلغ را بفرست، واریز کن و عکس رسید را همین‌جا بفرست."
    )
    btns = [
        [{"text": "➕ شارژ", "callback_data": "u:charge"}, {"text": "🧾 تاریخچه", "callback_data": "u:history"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "u:home"}],
    ]
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _menu_charge_amount(chat_id, msg_id=None):
    cfg = _cfg()
    txt = (
        "💳 <b>شارژ کیف پول</b>\n"
        "مرحله ۱ از ۲ · مبلغ واریز\n\n"
        "─ ─ ─ ─ ─ ─ ─ ─\n"
        "مبلغ را به <b>تومان</b> بفرست یا یکی از دکمه‌های آماده را بزن.\n"
        "اعداد فارسی، ویرگول و «تومان» هم خوانده می‌شود.\n\n"
        f"مثال: <code>100000</code>\n"
        f"حداقل: {cfg.get('min_charge'):,} تومان"
    )
    btns = [
        [{"text": "100,000", "callback_data": "u:amt:100000"},
         {"text": "200,000", "callback_data": "u:amt:200000"}],
        [{"text": "500,000", "callback_data": "u:amt:500000"},
         {"text": "1,000,000", "callback_data": "u:amt:1000000"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "u:home"}],
    ]
    BOT_STATE[chat_id] = {"step": "charge_amount", "data": {}}
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _menu_charge_pay(chat_id, msg_id, amount, tg_user):
    cfg = _cfg()
    BOT_STATE[chat_id] = {"step": "charge_receipt", "data": {"amount": int(amount)}}
    card = str(cfg.get("card_number") or "").strip()
    owner = str(cfg.get("card_owner") or "").strip()
    if not card:
        txt = (
            f"⚠️ شماره کارت هنوز در پنل تنظیم نشده.\n\n"
            f"مبلغ انتخابی: <b>{_toman(amount)} تومان</b>\n"
            f"به پشتیبانی پیام بده تا شماره کارت را بگیری."
        )
        btns = [[{"text": "🛟 پشتیبانی", "callback_data": "u:support"},
                 {"text": "🏠 منو", "callback_data": "u:home"}]]
        if msg_id:
            await _edit(chat_id, msg_id, txt, buttons=btns)
        else:
            await _send(chat_id, txt, buttons=btns)
        return
    txt = (
        f"💳 <b>شارژ کیف پول</b>\n"
        f"مرحله ۲ از ۲ · واریز\n\n"
        f"🔻 لطفاً مبلغ <b>{_toman(amount)} تومان</b> را به شماره کارت زیر واریز کن:\n\n"
        f"⚠️ دقت کن قیمت‌ها به «تومان» است!\n\n"
        f"<code>{_html(card)}</code>\n"
        f"به نام: {_html(owner)}\n\n"
        "برای کپی شماره کارت روی دکمه پایین بزن 👇\n\n"
        "─ ─ ─ ─ ─ ─ ─ ─\n\n"
        "✅ <b>خیلی مهم:</b> حتماً دقیقاً همین مبلغ را واریز کن. اگر کمتر یا بیشتر واریز شود، تأیید ممکن است با تأخیر انجام شود."
    )
    btns = [
        [{"text": "📋 کپی شماره کارت", "callback_data": "u:cardcopy"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "u:home"}],
    ]
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)
    await _send(chat_id,
                "🧾 حالا <b>عکس رسید</b> یا <b>شماره پیگیری</b> را همین‌جا بفرست.\n"
                "به محض تأیید ادمین، موجودی کیف پولت شارژ می‌شود.")


async def _submit_receipt(chat_id, tg_user, payload: dict):
    """payload may contain 'photo' file_id or 'text' tracking number."""
    cfg = _cfg()
    st = BOT_STATE.get(chat_id)
    amount = int((st or {}).get("data", {}).get("amount") or 0) if st else 0
    rid = "C" + secrets.token_hex(3).upper()
    amount = int(amount or 0)
    BOT_STATE.pop(chat_id, None)

    charges = dict(cfg["charges"])
    charges[rid] = {
        "tg_id": str(chat_id),
        "tg_name": str(tg_user.get("first_name") or tg_user.get("username") or chat_id)[:40],
        "amount": amount,
        "note": "رسید تصویر" if payload.get("photo") else "شماره پیگیری / متنی",
        "status": "pending",
        "ts": time.time(),
        "photo": payload.get("photo") or "",
    }
    await _save_cfg(charges=charges)

    if amount:
        await _send(chat_id,
                    f"🧾 <b>خلاصه پرداخت</b>\n\n"
                    f"🔖 شناسه رسید: <code>{rid}</code>\n"
                    f"💰 مبلغ: <b>{_toman(amount)} تومان</b>\n\n"
                    f"📌 وضعیت: <b>فیش ثبت شد و در صف بررسی است ✅</b>\n\n"
                    f"🔎 مرحله بعد: فیش دقیق بررسی می‌شود و بعد از تأیید، موجودی کیف پولت شارژ می‌شود.\n"
                    f"⏳ زمان بررسی: از چند دقیقه تا چند ساعت (بسته به حجم درخواست‌ها).",
                    buttons=[[{"text": "🏠 منو", "callback_data": "u:home"}]])
    else:
        await _send(chat_id, "⚠️ مبلغ رسید ثبت نشد. دوباره از «➕ شارژ» شروع کن.")

    adm = "، ".join(str(a) for a in (cfg.get("admin_ids") or [])) or ""
    cap = (
        f"🧾 <b>رسید جدید</b>\n\n"
        f"👤 کاربر: <code>{_html(str(tg_user.get('first_name') or '') or '')}</code> {_html(str(tg_user.get('username') or '') or '')}\n"
        f"🆔 تلگرام: <code>{chat_id}</code>\n"
        f"🔖 شناسه: <code>{rid}</code>\n"
        f"💰 مبلغ: <b>{_toman(amount)} تومان</b>\n"
        f"📄 توضیح: {payload.get('note') or ''}\n"
        f"🕐 {time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time()))}"
    )
    btns = [
        [{"text": "✅ تأیید", "callback_data": f"adm:{rid}:ok"},
         {"text": "❌ رد", "callback_data": f"adm:{rid}:no"}],
    ]
    for a in (cfg.get("admin_ids") or []):
        try:
            if payload.get("photo"):
                await _forward_photo(int(a), payload["photo"], caption=cap, buttons=btns)
            else:
                await _send(int(a), cap, buttons=btns)
        except Exception as exc:
            logger.warning("charge notify admin %s failed: %s", a, exc)

    if not (cfg.get("admin_ids")):
        try:
            await _send(chat_id, "⚠️ هنوز ادمینی برای بررسی رسید تنظیم نشده. به پشتیبانی پیام بده.")
        # FIXME: [auto-fix]: handle exception


async def _admin_approve(rid, admin_id):
    cfg = _cfg()
    charges = dict(cfg["charges"])
    ch = charges.get(rid)
    if not ch or ch.get("status") != "pending":
        return False, "دیگر در صف نیست"
    ch["status"] = "approved"
    ch["reviewed_by"] = str(admin_id)
    charges[rid] = ch
    await _save_cfg(charges=charges)
    await _wallet_add(ch["tg_id"], int(ch.get("amount") or 0), f"شارژ کارت‌به‌کارت (رسید {rid})")
    try:
        bal = await _wallet_balance(ch["tg_id"])
        await _send(int(ch["tg_id"]),
                    f"✅ رسید <code>{rid}</code> تأیید شد!\n"
                    f"💰 +<b>{_toman(ch.get('amount'))} تومان</b> به کیف پول اضافه شد.\n"
                    f"💳 موجودی: <b>{_toman(bal)} تومان</b>",
                    buttons=[[{"text": "🛒 برو به فروشگاه", "callback_data": "u:buy"},
                              {"text": "💳 کیف پول", "callback_data": "u:wallet"}]])
    # FIXME: [auto-fix]: handle exception
    return True, "تأیید شد"


async def _admin_reject(rid, admin_id):
    cfg = _cfg()
    charges = dict(cfg["charges"])
    ch = charges.get(rid)
    if not ch or ch.get("status") != "pending":
        return False, "دیگر در صف نیست"
    ch["status"] = "rejected"
    ch["reviewed_by"] = str(admin_id)
    charges[rid] = ch
    await _save_cfg(charges=charges)
    try:
        await _send(int(ch["tg_id"]),
                    f"❌ رسید <code>{rid}</code> رد شد.\n"
                    "در صورت اشکال، از بخش پشتیبانی پیام بده.",
                    buttons=[[{"text": "🛟 پشتیبانی", "callback_data": "u:support"}]])
    # FIXME: [auto-fix]: handle exception
    return True, "رد شد"


async def _menu_history(chat_id, msg_id=None):
    cfg = _cfg()
    txns = (cfg.get("txns") or {}).get(str(chat_id), [])
    if not txns:
        txt = "🧾 تاریخچه هنوز خالی است.\nاولین شارژت را انجام بده!"
        btns = [[{"text": "➕ شارژ", "callback_data": "u:charge"}],
                [{"text": "🔙 کیف پول", "callback_data": "u:wallet"}]]
    else:
        rows = []
        for t in reversed(txns[-15:]):
            sign = "+" if int(t.get("amount", 0)) > 0 else "−"
            typ = "شارژ" if t.get("type") == "deposit" else "خرید"
            rows.append(f"{sign}{_toman(abs(int(t.get('amount', 0))))} تومان · {typ} — {t.get('note') or ''}")
        txt = "🧾 <b>تاریخچه تراکنش‌ها</b>\n\n" + "\n".join(rows)
        btns = [[{"text": "🔙 کیف پول", "callback_data": "u:wallet"}]]
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _menu_trial(chat_id, msg_id, tg_user):
    cfg = _cfg()
    used = cfg.get("_trial_used") or []
    if str(chat_id) in used:
        txt = "⚠️ سهمیه تست رایگانت قبلاً استفاده شده.\nبرای ادامه از فروشگاه پلن بخر."
        btns = [[{"text": "🛒 فروشگاه", "callback_data": "u:buy"},
                 {"text": "🏠 منو", "callback_data": "u:home"}]]
        await _edit(chat_id, msg_id, txt, buttons=btns)
        return
    gb = cfg.get("trial_gb") or 5
    days = cfg.get("trial_days") or 3
    u, created = await _extend_user(chat_id, tg_user, gb, days, "تست رایگان")
    used = list(cfg.get("_trial_used") or [])
    used.append(str(chat_id))
    await _save_cfg(_trial_used=used)
    await _edit(chat_id, msg_id, "⏳ در حال ساخت اشتراک آزمایشی...", buttons=[])
    try:
        r = await _sub_report(u)
        link = r["sub_url"]
    except Exception:
        link = ""
    txt = (
        f"🎁 <b>تست رایگان روشن شد!</b>\n\n"
        f"⚡ حجم: <b>{gb} GB</b>\n"
        f"⏳ مدت: <b>{days} روز</b>\n\n"
        f"🔗 لینک را لمس کن و در اپ وارد کن:\n"
        f"<code>{_html(link)}</code>\n\n"
        "این سهمیه یک‌بار است. برای ادامه از فروشگاه پلن بخر 🚀"
    )
    await _send(chat_id, txt, buttons=[[{"text": "📋 سرویس‌ها", "callback_data": "u:services"},
                                        {"text": "🏠 منو", "callback_data": "u:home"}]])
    try:
        if r and r.get("vless"):
            await _send_photo(chat_id, _qrcode_png(r["vless"]), caption="QR کانفیگ تست رایگان")
    # FIXME: [auto-fix]: handle exception


async def _menu_gift(chat_id, msg_id=None):
    BOT_STATE[chat_id] = {"step": "gift", "data": {}}
    txt = "🎁 <b>کد هدیه</b>\n\nکد هدیه‌ات را بفرست؛ به حسابت حجم و زمان اضافه می‌شود.\n(برای انصراف /cancel)"
    btns = [[{"text": "🏠 منوی اصلی", "callback_data": "u:home"}]]
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _apply_gift(chat_id, tg_user, code):
    cfg = _cfg()
    code = str(code or "").strip().upper()
    gifts = dict(cfg["gift_codes"])
    g = gifts.get(code)
    if not g:
        await _send(chat_id, "❌ این کد هدیه معتبر نیست.\nکد را دقیق بفرست یا از پشتیبانی بپرس.",
                    buttons=[[{"text": "🛟 پشتیبانی", "callback_data": "u:support"}]])
        return
    if g.get("used_by"):
        await _send(chat_id, "⚠️ این کد قبلاً استفاده شده است.",
                    buttons=[[{"text": "🏠 منو", "callback_data": "u:home"}]])
        return
    g["used_by"] = str(chat_id)
    g["used_at"] = time.time()
    gifts[code] = g
    await _save_cfg(gift_codes=gifts)
    u, _created = await _extend_user(chat_id, tg_user, g.get("gb") or 0, g.get("days") or 0, "کد هدیه")
    txt = (
        f"🎁 کد هدیه اعمال شد!\n\n"
        f"⚡ +{g.get('gb')} GB\n"
        f"⏳ +{g.get('days')} روز\n\n"
        "🎉 هدیه‌ات به حسابت اضافه شد."
    )
    await _send(chat_id, txt, buttons=[[{"text": "📋 سرویس‌های من", "callback_data": "u:services"},
                                        {"text": "🏠 منو", "callback_data": "u:home"}]])


async def _menu_account(chat_id, msg_id=None, tg_user=None):
    rec = _ref_record(chat_id)
    cfg = _cfg()
    uid, user = await _bound_user(chat_id)
    created_at = "—"
    if user and user.get("created_at"):
        try:
            created_at = str(P.datetime.fromisoformat(str(user["created_at"])).strftime("%Y-%m-%d"))
        except Exception:
            created_at = str(user.get("created_at") or "—")[:10]
    bu = await _bot_username()
    inv = f"\n🔗 لینک دعوت: <code>https://t.me/{bu}</code>" if bu else ""
    txt = (
        "👤 <b>پروفایل حساب</b>\n\n"
        f"🆔 شناسه تلگرام: <code>{chat_id}</code>\n"
        f"📅 عضویت از: <b>{created_at}</b>\n\n"
        f"🎟 <b>کد معرفی تو</b>\n"
        f"<code>{rec.get('code')}</code>\n"
        f"📊 دعوت‌های موفق: <b>{rec.get('count') or 0}</b>\n\n"
        f"این کد را برای دوستانت بفرست؛ از اولین خریدشان <b>{cfg.get('referral_percent')}%</b> پاداش می‌گیری. 🚀"
        + inv
    )
    btns = [[{"text": "🎟 پاداش‌ها و معرفی", "callback_data": "u:referral"},
             {"text": "🏠 منو", "callback_data": "u:home"}]]
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _menu_referral(chat_id, msg_id=None):
    rec = _ref_record(chat_id)
    cfg = _cfg()
    bal = await _wallet_balance(chat_id)
    bu = await _bot_username()
    link = f"https://t.me/{bu}?start={rec.get('code')}" if bu else f"کد: {rec.get('code')}"
    txt = (
        "🎟 <b>پاداش‌ها و معرفی</b>\n\n"
        f"💳 موجودی کیف پول: <b>{_toman(bal)} تومان</b>\n"
        f"📊 دعوت‌های موفق: <b>{rec.get('count') or 0}</b>\n"
        f"🎁 پاداش هر خرید اول: <b>{cfg.get('referral_percent')}٪</b> از مبلغ\n\n"
        f"🎟 کد اختصاصی تو: <code>{_html(rec.get('code'))}</code>\n"
        f"🔗 لینک دعوت:\n<code>{_html(link)}</code>\n\n"
        "پاداش بعد از اولین خرید پولی دوستت، خودکار به کیف پولت واریز می‌شود."
    )
    btns = [{"text": "👤 حساب من", "callback_data": "u:account"},
            {"text": "🏠 منو", "callback_data": "u:home"}]
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _menu_support(chat_id, msg_id=None):
    cfg = _cfg()
    txt = cfg.get("support_text") or "🛟 <b>پشتیبانی</b>\n\nبا ادمین در ارتباط باش."
    btns = [[{"text": "🏠 منوی اصلی", "callback_data": "u:home"}]]
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _menu_links(chat_id, msg_id):
    uid, user = await _bound_user(chat_id)
    if not uid:
        txt = "⚠️ اول یک سرویس بگیر."
        btns = [[{"text": "🛒 خرید", "callback_data": "u:buy"}]]
        if msg_id:
            await _edit(chat_id, msg_id, txt, buttons=btns)
        else:
            await _send(chat_id, txt, buttons=btns)
        return
    try:
        r = await _sub_report(user)
        base = r["sub_url"]
    except Exception:
        base = ""
    if not base:
        txt = "⛔ لینک ساب در دسترس نیست."
        btns = [[{"text": "🏠 منو", "callback_data": "u:home"}]]
        if msg_id:
            await _edit(chat_id, msg_id, txt, buttons=btns)
        else:
            await _send(chat_id, txt, buttons=btns)
        return
    txt = (
        "🔗 <b>لینک‌های اشتراک (Sub)</b>\n\n"
        "دریافت خودکار — در Hiddify / v2rayNG / Streisand از «افزودن از کلیپ‌بورد» وارد کن:\n\n"
        f"📦 <b>پیش‌فرض (base64):</b>\n<code>{_html(base)}</code>\n\n"
        f"👑 <b>Clash / Meta:</b>\n<code>{_html(base)}?format=clash</code>\n\n"
        f"📦 <b>Sing-box:</b>\n<code>{_html(base)}?format=singbox</code>\n\n"
        f"🖥 <b>v2rayN (vjson):</b>\n<code>{_html(base)}?format=vjson</code>"
    )
    btns = [[{"text": "🏠 منوی اصلی", "callback_data": "u:home"}]]
    if msg_id:
        await _edit(chat_id, msg_id, txt, buttons=btns)
    else:
        await _send(chat_id, txt, buttons=btns)


async def _menu_toggle_service(chat_id, msg_id):
    uid, user = await _bound_user(chat_id)
    if not uid:
        await _edit(chat_id, msg_id, "⚠️ سرویسی برای مدیریت نیست.",
                    buttons=[[{"text": "🛒 خرید", "callback_data": "u:buy"}]])
        return
    cuuid = user.get("config_uuid")
    old = str(user.get("status") or "active").lower()
    new = "disabled" if old != "disabled" else "active"
    async with P.USERS_LOCK:
        if uid in P.USERS:
            P.USERS[uid]["status"] = new
    if cuuid:
        async with P.LINKS_LOCK:
            if cuuid in P.LINKS:
                P.LINKS[cuuid]["active"] = new == "active"
    await P.save_state()
    if P.WORKER.get("connected") is True and (cuuid in P.LINKS and P._user_uses_worker_inbound(P.USERS.get(uid, {}))):
        asyncio.create_task(P._worker_sync_users())
    P.log_activity("user", f"سرویس «{user.get('username')}» از ربات {'متوقف' if new=='disabled' else 'فعال'} شد",
                   "warn" if new == "disabled" else "ok")
    txt = f"✅ سرویس به حالت «{('⏸ متوقف' if new == 'disabled' else '▶️ فعال')}» تغییر کرد."
    await _edit(chat_id, msg_id, txt, buttons=[[{"text": "📋 سرویس‌های من", "callback_data": "u:services"}]])


async def _menu_delete_service(chat_id, msg_id, tg_user):
    uid, user = await _bound_user(chat_id)
    if not uid:
        return
    cuuid = user.get("config_uuid")
    async with P.USERS_LOCK:
        P.USERS.pop(uid, None)
    if cuuid:
        async with P.LINKS_LOCK:
            P.LINKS.pop(cuuid, None)
            for k in list(P.PATH_INDEX.keys()):
                if P.PATH_INDEX[k] == cuuid:
                    P.PATH_INDEX.pop(k, None)
    await P.save_state()
    if P.WORKER.get("connected") is True and P._user_uses_worker_inbound(user):
        asyncio.create_task(P._worker_sync_users())
    P.log_activity("user", f"سرویس «{user.get('username')}» از ربات حذف شد", "err")
    await _edit(chat_id, msg_id, "🗑 سرویس شما حذف شد. هر وقت خواستی دوباره بخر یا تست رایگان بگیر.",
                buttons=[[{"text": "🛒 خرید", "callback_data": "u:buy"},
                          {"text": "🏠 منو", "callback_data": "u:home"}]])


# ── Message rendering (legacy quick views) ──────────────────────────────────
def _fmt_subscription(r: dict) -> str:
    lines = [
        "📱 <b>اشتراک شما</b>\n",
        f"👤 نام کاربری: <code>{_html(r['username'])}</code>",
        f"📶 وضعیت: {_status_icon(r['status'], r['is_active'], r['used'], r['limit'])}",
        f"⚡ مصرف: <code>{_html(r['used_fmt'])}</code> از <code>{_html(r['limit_fmt'])}</code>"
        + (f" ({r['pct']}%)" if r['limit'] else ""),
    ]
    if r.get("expire_at"):
        exp = r["expire_at"].replace("T", " ").split(".")[0]
        lines.append(f"⏳ انقضا: <code>{_html(exp)}</code>"
                     + (f" ({r['expire_days']} روز مانده)" if r.get("expire_days") is not None else ""))
    else:
        lines.append("⏳ انقضا: نامحدود ♾")
    if r.get("worker_count"):
        lines.append(f"🌍 کانفیگ Worker (چندلوکیشن): {r['worker_count']} عدد")
    lines += [
        "",
        "🔗 <b>لینک سابسکریپشن:</b>",
        f"<code>{_html(r['sub_url'])}</code>",
        "این لینک را در v2rayNG / v2rayTun / Streisand / Hiddify اضافه کنید.",
    ]
    return "\n".join(lines)


def _fmt_config(r: dict, include_sub=True) -> str:
    lines = [
        "⚙️ <b>کانفیگ شما</b>\n",
        f"👤 کاربر: <code>{_html(r['username'])}</code>",
        "",
        "📄 <b>کانفیگ VLESS:</b>",
        f"<code>{_html(r['vless'])}</code>",
    ]
    if len(r.get("configs", [])) > 1:
        lines.append(f"📚 تعداد کل کانفیگ‌های سابسکریپشن: {len(r['configs'])}")
    if include_sub:
        lines += ["", f"🔗 ساب: <code>{_html(r['sub_url'])}</code>"]
    return "\n".join(lines)


async def _send_sub(chat_id, user, with_qr=True):
    """Send the full subscription package (status + config + QR)."""
    try:
        r = await _sub_report(user)
    except Exception:
        await _send(chat_id, "⛔ اشتراکی برای این حساب پیدا نشد. از «🕹 منو» استفاده کنید.")
        return
    await _send(chat_id, _fmt_subscription(r))
    await _send(chat_id, _fmt_config(r, include_sub=False))
    if with_qr and r.get("vless"):
        try:
            await _send_photo(chat_id, _qrcode_png(r["vless"]), caption=f"QR کانفیگ {_html(r['username'])[:40]}")
        except Exception as exc:
            logger.warning("qr send failed: %s", exc)


# ── Command handlers ────────────────────────────────────────────────────────
async def _cmd_start(chat_id, tg_user, is_admin, text=""):
    cfg = _cfg()
    _, user = await _bound_user(chat_id)
    rec = _ref_record(chat_id)
    # referral code via /start <code>
    if text and text.startswith("/start "):
        payload = str(text.split(maxsplit=1)[1]).strip()
        if payload.lower() not in ("s", "menu") and not payload.isdigit() and not rec.get("invited_by"):
            owner = await _ref_owner_by_code(payload)
            if owner and owner != str(chat_id):
                rec["invited_by"] = owner
    await _save_ref_record(chat_id, rec)
    welcome = cfg.get("welcome_msg") or "به ربات خوش آمدید 👋"
    if _is_admin(chat_id):
        extra = "\n\n(شما ادمین هستید — از منوی ادمین هم می‌توانید استفاده کنید.)"
    elif user:
        extra = f"\n\n✅ حساب شما: <code>{_html(user.get('username'))}</code>"
    else:
        extra = ""
    menu = ADMIN_MENU if is_admin else USER_MENU
    await _send_menu(chat_id, welcome + extra, menu)
    await _menu_home(chat_id)


async def _cmd_subscribe(chat_id, tg_user, is_admin):
    uid, user = await _bound_user(chat_id)
    if uid:
        await _send(chat_id, "📌 شما قبلاً یک اشتراک در پنل دارید؛ وضعیت آن 👇")
        try:
            txt, btns = await _service_menu(chat_id)
            await _send(chat_id, txt, buttons=btns)
        except Exception:
            await _send_sub(chat_id, user, with_qr=False)
    else:
        await _send(chat_id, "🔄 در حال ساخت اشتراک شما...")
        try:
            username = str(tg_user.get("username") or "").strip().lower() or f"tg-{chat_id}"
            _, user, created = await _create_sub_user(chat_id, tg_user, username)
        except Exception as exc:
            logger.exception("bot subscribe failed")
            await _send(chat_id, f"⛔ ساخت اشتراک ناموفق بود: {_html(str(exc)[:120])}")
            return
        await _send(chat_id, "✅ <b>اشتراک شما ساخته شد!</b>")
        await _send_sub(chat_id, user, with_qr=True)


async def _cmd_my_sub(chat_id):
    uid, user = await _bound_user(chat_id)
    if not uid:
        await _send(chat_id, "⚠️ هنوز اشتراکی ندارید.\nاز «🕹 منو» گزینه «🎁 تست رایگان» را بزنید.",
                    buttons=[[{"text": "🛒 خرید", "callback_data": "u:buy"},
                              {"text": "🎁 تست رایگان", "callback_data": "u:trial"}]])
        return
    try:
        txt, btns = await _service_menu(chat_id)
    except Exception:
        await _send_sub(chat_id, user, with_qr=False)
        return
    await _send(chat_id, txt, buttons=btns)


async def _cmd_sub_link(chat_id):
    uid, user = await _bound_user(chat_id)
    if not uid:
        await _send(chat_id, "⚠️ هنوز اشتراکی ندارید.\nاز «🕹 منو» شروع کنید.")
        return
    await _menu_links(chat_id, None)


async def _cmd_qr(chat_id):
    uid, user = await _bound_user(chat_id)
    if not uid:
        await _send(chat_id, "⚠️ ابتدا یک اشتراک بگیرید.")
        return
    try:
        r = await _sub_report(user)
    except Exception:
        await _send(chat_id, "⛔ اشتراکی پیدا نشد.")
        return
    if not r.get("vless"):
        await _send(chat_id, "⛔ کانفیگی برای ساخت QR موجود نیست.")
        return
    await _send_photo(chat_id, _qrcode_png(r["vless"]), caption=f"QR کانفیگ {_html(r['username'])[:40]}")


async def _cmd_help(chat_id):
    lines = [
        "📖 <b>راهنما</b>\n",
        "🕹 <b>منو</b> — منوی اصلی ربات",
        "🛒 <b>خرید</b> — خرید پلن از فروشگاه",
        "💳 <b>کیف پول</b> — شارژ کارت‌به‌کارت و خرید",
        "🎁 <b>کد هدیه</b> — اعمال کد هدیه",
        "🛟 <b>پشتیبانی</b> — ارتباط با ادمین",
        "",
        "📲 اپ‌های پیشنهادی: v2rayNG، Streisand، Hiddify، V2Box.",
    ]
    await _send(chat_id, "\n".join(lines))


async def _cmd_trial(chat_id, tg_user):
    """Trigger a one-time free trial for a brand-new bot user."""
    cfg = _cfg()
    used = cfg.get("_trial_used") or []
    if str(chat_id) in used:
        await _send(chat_id, "⚠️ سهمیه تست رایگانت قبلاً استفاده شده.\nاز فروشگاه پلن بخر.",
                    buttons=[[{"text": "🛒 خرید", "callback_data": "u:buy"}]])
        return
    gb = cfg.get("trial_gb") or 5
    days = cfg.get("trial_days") or 3
    u, _ = await _extend_user(chat_id, tg_user, gb, days, "تست رایگان")
    used = list(cfg.get("_trial_used") or [])
    used.append(str(chat_id))
    await _save_cfg(_trial_used=used)
    try:
        r = await _sub_report(u)
        link = r["sub_url"]
    except Exception:
        link = ""
    await _send(chat_id,
                f"🎁 <b>تست رایگان روشن شد!</b>\n\n⚡ {gb} GB · ⏳ {days} روز\n\n"
                f"🔗 <code>{_html(link)}</code>\n\nاین سهمیه یک‌بار است.",
                buttons=[[{"text": "📋 سرویس‌های من", "callback_data": "u:services"}]])


# ── Admin handlers ──────────────────────────────────────────────────────────
async def _cmd_stats(chat_id):
    async with P.USERS_LOCK:
        users = dict(P.USERS)
    async with P.LINKS_LOCK:
        links = dict(P.LINKS)
    total_traffic = sum(int(u.get("traffic_used_bytes") or 0) for u in users.values())
    limit_traffic = sum(int(u.get("traffic_limit_bytes") or 0) for u in users.values())
    active = sum(1 for u in users.values() if P.is_user_allowed(u))
    expired = sum(1 for u in users.values() if str(u.get("status") or "").lower() == "expired")
    disabled = sum(1 for u in users.values() if str(u.get("status") or "").lower() == "disabled")
    connected = sum(1 for c in P.connections.values() if c.get("uuid"))
    cfg = _cfg()
    pending = sum(1 for c in (cfg.get("charges") or {}).values() if c.get("status") == "pending")
    lines = [
        "📈 <b>آمار پنل</b>\n",
        f"👥 کاربران: <b>{len(users)}</b>",
        f"✅ فعال: <b>{active}</b>   ⏳ منقضی: <b>{expired}</b>   ⛔ غیرفعال: <b>{disabled}</b>",
        f"🔌 اتصالات فعلی: <b>{connected}</b>",
        f"⚡ مصرف کل: <b>{P.fmt_bytes(total_traffic)}</b> از <b>{P.fmt_bytes(limit_traffic)}</b>",
        f"🔗 لینک‌ها: <b>{len(links)}</b>",
        f"🧾 رسیدهای در انتظار: <b>{pending}</b>",
        "",
        f"🌐 Worker متصل: {'✅' if P.WORKER.get('connected') is True else '⛔'}",
        f"ℹ️ آپ‌تایم: {P.uptime()}",
    ]
    await _send(chat_id, "\n".join(lines))


async def _cmd_add_user(chat_id):
    if BOT_STATE.get(chat_id, {}).get("step"):
        await _send(chat_id, "⚠️ یک عملیات در حال انجام است؛ اول آن را کامل کنید یا /cancel بزنید.")
        return
    BOT_STATE[chat_id] = {"step": "add_name", "data": {}}
    await _send(chat_id, "➕ نام کاربری جدید را ارسال کنید:\n(مثلاً <code>ali</code>)")


async def _cmd_add_direct(chat_id, args):
    parts = [x.strip() for x in args.split() if x.strip()]
    if len(parts) < 3:
        await _send(chat_id, "کاربرد: <code>/add NAME GB DAYS</code>\nمثال: <code>/add ali 40 30</code>")
        return
    name, gb, days = parts[0], parts[1], parts[2]
    try:
        gb_f = float(gb.replace(",", "."))
        d_i = int(days)
    except Exception:
        await _send(chat_id, "حجم و روزها باید عدد باشند. مثال: <code>/add ali 40 30</code>")
        return
    _, user, created = await _create_sub_user(chat_id, {"username": name.lower(), "first_name": name}, name, gb_f, d_i)
    if created:
        await _send(chat_id, f"✅ کاربر <b>{_html(name)}</b> با {gb_f:g}GB و {d_i} روز ساخته شد.")
        await _send_sub(chat_id, user, with_qr=True)
    else:
        _sub = P.get_bot_sub_link(user.get("config_uuid") or "") or P.sub_hash_url(user.get("config_uuid") or "")
        extra = f"\nساب: <code>{_html(_sub)}</code>" if _sub else ""
        await _send(chat_id, f"⚠️ این تلگرام از قبل کاربر <b>{_html(user.get('username'))}</b> را دارد.{extra}")


async def _cmd_toggle(chat_id, args):
    ref = args.strip()
    uid, u = await _find_user_by_ref(ref)
    if not uid:
        await _send(chat_id, f"⛔ کاربر «{_html(ref)}» پیدا نشد.")
        return
    old = u.get("status") or "active"
    new = "disabled" if old != "disabled" else "active"
    async with P.USERS_LOCK:
        if uid in P.USERS:
            P.USERS[uid]["status"] = new
    cuuid = u.get("config_uuid")
    if cuuid:
        async with P.LINKS_LOCK:
            if cuuid in P.LINKS:
                P.LINKS[cuuid]["active"] = new == "active"
    await P.save_state()
    if P.WORKER.get("connected") is True and (cuuid in P.LINKS and P._user_uses_worker_inbound(P.USERS.get(uid, {}))):
        asyncio.create_task(P._worker_sync_users())
    P.log_activity("user", f"کاربر «{u.get('username')}» از ربات {'غیرفعال' if new=='disabled' else 'فعال'} شد", "warn" if new=="disabled" else "ok")
    await _send(chat_id, f"👤 {_html(u.get('username'))} → وضعیت: <b>{'⚡ فعال' if new=='active' else '⛔ غیرفعال'}</b>")


async def _cmd_reset(chat_id, args):
    ref = args.strip()
    uid, u = await _find_user_by_ref(ref)
    if not uid:
        await _send(chat_id, f"⛔ کاربر «{_html(ref)}» پیدا نشد.")
        return
    async with P.USERS_LOCK:
        if uid in P.USERS:
            P.USERS[uid]["traffic_used_bytes"] = 0
    cuuid = u.get("config_uuid")
    if cuuid:
        async with P.LINKS_LOCK:
            if cuuid in P.LINKS:
                P.LINKS[cuuid]["used_bytes"] = 0
    await P.save_state()
    if P.WORKER.get("connected") is True and (cuuid in P.LINKS and P._user_uses_worker_inbound(P.USERS.get(uid, {}))):
        asyncio.create_task(P._worker_sync_users())
    P.log_activity("user", f"مصرف کاربر «{u.get('username')}» از ربات ریست شد", "info")
    await _send(chat_id, f"✅ مصرف کاربر <b>{_html(u.get('username'))}</b> صفر شد.")


async def _cmd_extend(chat_id, args):
    parts = args.split()
    if len(parts) < 2:
        await _send(chat_id, "کاربرد: <code>/extend USERNAME DAYS</code>")
        return
    ref, days = parts[0], parts[1]
    uid, u = await _find_user_by_ref(ref)
    if not uid:
        await _send(chat_id, f"⛔ کاربر «{_html(ref)}» پیدا نشد.")
        return
    try:
        d = int(days)
    except Exception:
        await _send(chat_id, "تعداد روز باید عدد باشد.")
        return
    now = P.datetime.now()
    exp = u.get("expire_at")
    try:
        base = P.datetime.fromisoformat(exp) if exp else now
    except Exception:
        base = now
    if base < now:
        base = now
    new_exp = base + P.timedelta(days=d)
    async with P.USERS_LOCK:
        if uid in P.USERS:
            P.USERS[uid]["expire_at"] = new_exp.isoformat()
            if P.USERS[uid].get("status", "active") == "expired":
                P.USERS[uid]["status"] = "active"
    cuuid = u.get("config_uuid")
    if cuuid:
        async with P.LINKS_LOCK:
            if cuuid in P.LINKS:
                P.LINKS[cuuid]["expires_at"] = new_exp.isoformat()
                P.LINKS[cuuid]["active"] = True
    await P.save_state()
    if P.WORKER.get("connected") is True and P._user_uses_worker_inbound(u):
        asyncio.create_task(P._worker_sync_users())
    P.log_activity("user", f"اشتراک «{u.get('username')}» به مدت {d} روز تمدید شد", "ok")
    await _send(chat_id, f"✅ اشتراک <b>{_html(u.get('username'))}</b> تا <code>{new_exp.isoformat()}</code> تمدید شد.")


async def _cmd_delete(chat_id, args):
    ref = args.strip()
    uid, u = await _find_user_by_ref(ref)
    if not uid:
        await _send(chat_id, f"⛔ کاربر «{_html(ref)}» پیدا نشد.")
        return
    cuuid = u.get("config_uuid")
    async with P.USERS_LOCK:
        P.USERS.pop(uid, None)
    if cuuid:
        async with P.LINKS_LOCK:
            P.LINKS.pop(cuuid, None)
            for k in list(P.PATH_INDEX.keys()):
                if P.PATH_INDEX[k] == cuuid:
                    P.PATH_INDEX.pop(k, None)
    await P.save_state()
    if P.WORKER.get("connected") is True and P._user_uses_worker_inbound(u):
        asyncio.create_task(P._worker_sync_users())
    P.log_activity("user", f"کاربر «{u.get('username')}» از ربات حذف شد", "err")
    await _send(chat_id, f"🗑 کاربر <b>{_html(u.get('username'))}</b> حذف شد.")


async def _cmd_users(chat_id, page=0):
    async with P.USERS_LOCK:
        items = list(P.USERS.items())
    items.sort(key=lambda x: str(x[1].get("created_at") or ""), reverse=True)
    per_page = 8
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page
    chunk = items[start:start + per_page]
    lines = ["👥 <b>لیست کاربران</b>\n"]
    if not chunk:
        lines.append("کاربری وجود ندارد.")
    for uid, u in chunk:
        st = _status_icon(u.get("status"), P.is_user_allowed(u), u.get("traffic_used_bytes", 0), u.get("traffic_limit_bytes", 0))
        lines.append(
            f"• <b>{_html(u.get('username'))}</b> [{uid}]\n"
            f"   ⚡ {P.fmt_bytes(u.get('traffic_used_bytes', 0))} / {('' if u.get('traffic_limit_bytes')==0 else P.fmt_bytes(u.get('traffic_limit_bytes')))}   "
            f"📶 {st}"
        )
    lines.append(f"\n📄 صفحه {page+1} از {total_pages} — {len(items)} کاربر")
    buttons = []
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"pg:{page-1}"})
    nav.append({"text": f"{page+1}/{total_pages}", "callback_data": "noop"})
    if page < total_pages - 1:
        nav.append({"text": "➡️", "callback_data": f"pg:{page+1}"})
    buttons.append(nav)
    LIST_PAGES[chat_id] = {"items": [u for _, u in items]}
    await _send(chat_id, "\n".join(lines), buttons=buttons)


async def _cmd_broadcast(chat_id):
    if BOT_STATE.get(chat_id, {}).get("step"):
        await _send(chat_id, "⚠️ یک عملیات در حال انجام است؛ اول آن را کامل کنید یا /cancel بزنید.")
        return
    BOT_STATE[chat_id] = {"step": "broadcast", "data": {}}
    await _send(chat_id, "📢 متن پیام همگانی را ارسال کنید (یا برای انصراف /cancel):")


async def _cmd_broadcast_direct(chat_id, text):
    targets = []
    async with P.USERS_LOCK:
        for uid, u in P.USERS.items():
            tid = str(u.get("telegram_id") or "")
            if tid and tid.isdigit():
                targets.append(int(tid))
    targets = list(dict.fromkeys(targets))
    sent = 0
    for t in targets:
        r = await _send(t, text)
        if r.get("ok"):
            sent += 1
        await asyncio.sleep(0.03)
    await _send(chat_id, f"📨 پیام همگانی ارسال شد به <b>{sent}</b> از <b>{len(targets)}</b> اکانت متصل.")


async def _cmd_gift(chat_id, args):
    parts = args.split()
    if len(parts) < 2:
        await _send(chat_id, "کاربرد: <code>/gift GB DAYS [COUNT]</code>\nمثال: <code>/gift 10 7</code> برای ۱۰ گیگ و ۷ روز")
        return
    try:
        gb = float(parts[0].replace(",", "."))
        days = int(parts[1])
        count = int(parts[2]) if len(parts) > 2 else 1
        count = max(1, min(int(count), 100))
    except Exception:
        await _send(chat_id, "ورودی عددی معتبر نیست. مثال: <code>/gift 10 7 3</code>")
        return
    cfg = _cfg()
    gifts = dict(cfg["gift_codes"])
    codes = []
    for _ in range(count):
        code = secrets.token_hex(4).upper()[:8]
        gifts[code] = {"gb": float(gb), "days": int(days), "used_by": None, "used_at": None,
                       "created_at": time.time()}
        codes.append(code)
    await _save_cfg(gift_codes=gifts)
    await _send(chat_id,
                f"🎁 <b>کدهای هدیه ساخته شد</b>\n\n{gb:g} GB · {days} روز · {count} عدد\n\n" +
                "\n".join(f"<code>{c}</code>" for c in codes) +
                "\n\nکاربران از دکمه «🎁 کد هدیه» در ربات استفاده می‌کنند.")


async def _cmd_plans(chat_id, args=""):
    cfg = _cfg()
    plans = cfg.get("plans") or []
    lines = ["🛍 <b>پلن‌های فروشگاه</b>\n"]
    if not plans:
        lines.append("پلنی تعریف نشده. با <code>/plan add NAME GB DAYS PRICE</code> اضافه کن.")
    for p in plans:
        lines.append(f"• <b>{_html(p.get('name'))}</b> [{p.get('id')}]\n   {p.get('gb')} GB · {p.get('days')} روز · {_toman(p.get('price'))} تومان")
    await _send(chat_id, "\n".join(lines))


async def _cmd_plan_add(chat_id, args):
    parts = args.split()
    if len(parts) < 4:
        await _send(chat_id, "کاربرد: <code>/plan add NAME GB DAYS PRICE</code>\nمثال: <code>/plan add VIP 100 30 350000</code>")
        return
    name = parts[0]
    try:
        gb = float(parts[1].replace(",", "."))
        days = int(parts[2])
        price = int(float(parts[3].replace(",", "")))
    except Exception:
        await _send(chat_id, "اعداد را درست وارد کن. مثال: <code>/plan add VIP 100 30 350000</code>")
        return
    cfg = _cfg()
    plans = list(cfg.get("plans") or [])
    pid = "p" + str(len(plans) + 1)
    plans.append({"id": pid, "name": name, "gb": gb, "days": days, "price": price})
    await _save_cfg(plans=plans)
    await _send(chat_id, f"✅ پلن <b>{_html(name)}</b> اضافه شد (پیش‌فرض: {gb:g}GB · {days} روز · {_toman(price)} تومان)")


async def _cmd_plan_del(chat_id, args):
    pid = args.strip()
    cfg = _cfg()
    plans = [p for p in (cfg.get("plans") or []) if str(p.get("id") or "") != pid]
    if len(plans) == len(cfg.get("plans") or []):
        await _send(chat_id, f"پلن با شناسه <code>{_html(pid)}</code> پیدا نشد.")
        return
    await _save_cfg(plans=plans)
    await _send(chat_id, f"🗑 پلن <code>{_html(pid)}</code> حذف شد.")


async def _cmd_setcard(chat_id, args):
    line = args.strip()
    m = re.match(r"^([\d\s-]+)\s+(.+)$", line)
    if not m:
        await _send(chat_id, "کاربرد: <code>/setcard 6037-XXXX-XXXX-XXXX نام صاحب حساب</code>")
        return
    card = "".join(ch for ch in m.group(1) if ch.isdigit())
    owner = m.group(2).strip()
    await _save_cfg(card_number=card, card_owner=owner)
    await _send(chat_id, f"✅ کارت تنظیم شد:\n<code>{_html(card)}</code>\nبه نام: {_html(owner)}")


async def _cmd_approve(chat_id, args=""):
    cfg = _cfg()
    pending = [(rid, c) for rid, c in (cfg.get("charges") or {}).items() if c.get("status") == "pending"]
    if not pending:
        await _send(chat_id, "🧾 رسیدی در انتظار نیست.")
        return
    txt = "🧾 <b>رسیدهای در انتظار بررسی</b>\n\n"
    btns = []
    for rid, c in pending[:12]:
        txt += f"• {_html(c.get('tg_name') or c.get('tg_id'))} — <b>{_toman(c.get('amount'))} تومان</b> · <code>{rid}</code>\n"
        btns.append([{"text": f"✅ {rid} · {_toman(c.get('amount'))}", "callback_data": f"adm:{rid}:ok"},
                     {"text": "❌", "callback_data": f"adm:{rid}:no"}])
    btns.append([{"text": "🏠 منو", "callback_data": "u:home"}])
    await _send(chat_id, txt, buttons=btns)


async def _cmd_manager(chat_id, text):
    lines = [
        "🔧 <b>دستورات مدیریتی</b>\n",
        "<code>/add NAME GB DAYS</code> — ساخت کاربر جدید",
        "<code>/users</code> — لیست کاربران",
        "<code>/toggle USER</code> — فعال/غیرفعال",
        "<code>/reset USER</code> — صفر کردن مصرف",
        "<code>/extend USER DAYS</code> — تمدید",
        "<code>/del USER</code> — حذف",
        "<code>/stats</code> — آمار پنل",
        "<code>/approve</code> — بررسی رسیدهای شارژ",
        "<code>/gift GB DAYS [COUNT]</code> — ساخت کد هدیه",
        "<code>/plans</code> — لیست پلن‌ها",
        "<code>/plan add NAME GB DAYS PRICE</code> — پلن جدید",
        "<code>/plan del ID</code> — حذف پلن",
        "<code>/setcard NUMBER OWNER</code> — تنظیم کارت شارژ",
        "<code>/broadcast TEXT</code> — پیام همگانی",
        "",
        "از دکمه‌های منو هم می‌توانید گام‌به‌گام استفاده کنید.",
    ]
    await _send(chat_id, "\n".join(lines))


async def _cmd_bot_settings(chat_id):
    cfg = _cfg()
    token = cfg.get("token") or ""
    masked = (token[:6] + "…" + token[-4:]) if len(token) > 12 else ("—" if not token else token)
    status = "✅ در حال اجرا" if (POLL_TASK and not POLL_TASK.done()) else "⛔ متوقف"
    admins = ", ".join(str(a) for a in cfg.get("admin_ids") or []) or "—"
    lines = [
        "⚙️ <b>تنظیمات ربات تلگرام</b>\n",
        f"وضعیت: {status}",
        f"توکن: <code>{_html(masked)}</code>",
        f"ادمین‌ها (ID): <code>{_html(admins)}</code>",
        f"کانال الزامی: {_html(cfg.get('channel') or '—')}",
        f"کارت شارژ: <code>{_html(cfg.get('card_number') or '—')}</code>",
        f"پلن‌ها: <b>{len(cfg.get('plans') or [])}</b>",
        "",
        "توکن را از @BotFather بگیرید و در پنل → Settings → تنظیمات ربات ست کنید.",
        "کارت، پلن و کد هدیه هم از همین ربات قابل تنظیم است.",
    ]
    buttons = [
        [{"text": "🔄 ریستارت ربات", "callback_data": "bot:restart"}],
        [{"text": "🧾 رسیدهای در انتظار", "callback_data": "adm:list"}],
    ]
    await _send(chat_id, "\n".join(lines), buttons=buttons)


# ── Multi-step flow continuation ────────────────────────────────────────────
async def _continue_state(chat_id, tg_user, step, text):
    cur = BOT_STATE.get(chat_id)
    if not cur:
        return
    s = cur.get("step")
    if s == "add_name" and cur.get("data", {}).get("name") is None:
        cur["data"]["name"] = str(text).strip()[:40]
        cur["step"] = "add_gb"
        await _send(chat_id, "حجم اشتراک را بر حسب <b>GB</b> وارد کنید:\n(مثلاً <code>40</code> برای ۴۰ گیگ)")
    elif s == "add_gb":
        try:
            cur["data"]["gb"] = float(_fa_digits(text).replace(",", ".").strip())
        except Exception:
            await _send(chat_id, "عدد معتبر نیست. دوباره حجم (GB) را وارد کنید.")
            return
        cur["step"] = "add_days"
        await _send(chat_id, "مدت اشتراک را به <b>روز</b> وارد کنید:\n(مثلاً <code>30</code> برای یک ماه)")
    elif s == "add_days":
        try:
            cur["data"]["days"] = int(_fa_digits(text).strip())
        except Exception:
            await _send(chat_id, "عدد معتبر نیست. دوباره تعداد روز را وارد کنید.")
            return
        data = cur["data"]
        BOT_STATE.pop(chat_id, None)
        name = data.get("name") or f"tg-{chat_id}"
        _, user, created = await _create_sub_user(chat_id, tg_user, name, data.get("gb") or 0, data.get("days") or 0)
        await _send(chat_id, f"✅ کاربر <b>{_html(name)}</b> با حجم {data.get('gb') or 0:g}GB و {data.get('days') or 0} روز ساخته شد.")
        await _send_sub(chat_id, user, with_qr=True)
    elif s == "broadcast":
        BOT_STATE.pop(chat_id, None)
        await _cmd_broadcast_direct(chat_id, text)
    elif s == "charge_amount":
        amount = _parse_money(text)
        if amount is None:
            await _send(chat_id, "مبلغ معتبر نیست. فقط عدد بفرست؛ مثلاً <code>100000</code>.")
            return
        cfg = _cfg()
        if amount < int(cfg.get("min_charge") or 0):
            await _send(chat_id, f"حداقل شارژ {cfg.get('min_charge'):,} تومان است. مبلغ بیشتری بفرست.")
            return
        if amount > int(cfg.get("max_charge") or 0):
            await _send(chat_id, f"حداکثر شارژ {cfg.get('max_charge'):,} تومان است.")
            return
        await _menu_charge_pay(chat_id, None, amount, tg_user)
    elif s == "charge_receipt":
        await _submit_receipt(chat_id, tg_user, {"text": text[:400]})
    elif s == "gift":
        BOT_STATE.pop(chat_id, None)
        await _apply_gift(chat_id, tg_user, text)
    else:
        BOT_STATE.pop(chat_id, None)


async def _cmd_cancel(chat_id):
    if BOT_STATE.pop(chat_id, None):
        await _send(chat_id, "✅ عملیات لغو شد.", buttons=[[{"text": "🏠 منو", "callback_data": "u:home"}]])
    else:
        await _send(chat_id, "عملیات در حال انتظاری وجود ندارد.")


# ── Callback dispatch ───────────────────────────────────────────────────────
async def _handle_u_callback(chat_id, cb_id, msg_id, data, from_user):
    tg_user = from_user or {}
    if data == "u:home":
        await _menu_home(chat_id)
        await _answer_cb(cb_id, "")
    elif data == "u:buy":
        await _menu_buy(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data.startswith("u:pkg:"):
        pid = data.split(":", 2)[2]
        plan = next((p for p in (_cfg().get("plans") or []) if str(p.get("id") or "") == pid), None)
        if not plan:
            await _answer_cb(cb_id, "پلن پیدا نشد", alert=True)
            return
        await _menu_confirm_buy(chat_id, msg_id, plan)
        await _answer_cb(cb_id, "")
    elif data.startswith("u:conf:"):
        pid = data.split(":", 2)[2]
        plan = next((p for p in (_cfg().get("plans") or []) if str(p.get("id") or "") == pid), None)
        if not plan:
            await _answer_cb(cb_id, "پلن پیدا نشد", alert=True)
            return
        await _answer_cb(cb_id, "🛒 در حال پرداخت...")
        await _exec_purchase(chat_id, tg_user, plan)
    elif data == "u:services":
        txt, btns = await _service_menu(chat_id)
        if msg_id:
            await _edit(chat_id, msg_id, txt, buttons=btns)
        else:
            await _send(chat_id, txt, buttons=btns)
        await _answer_cb(cb_id, "")
    elif data == "u:wallet":
        await _menu_wallet(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data == "u:charge":
        await _menu_charge_amount(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data.startswith("u:amt:"):
        try:
            amount = int(data.split(":", 2)[2])
        except Exception:
            amount = 0
        if amount <= 0:
            await _answer_cb(cb_id, "مبلغ نامعتبر", alert=True)
            return
        await _menu_charge_pay(chat_id, msg_id, amount, tg_user)
        await _answer_cb(cb_id, "")
    elif data == "u:cardcopy":
        cfg = _cfg()
        card = str(cfg.get("card_number") or "").strip()
        await _answer_cb(cb_id, card or "تنظیم نشده", alert=True)
    elif data == "u:history":
        await _menu_history(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data == "u:trial":
        await _menu_trial(chat_id, msg_id, tg_user)
        await _answer_cb(cb_id, "")
    elif data == "u:gift":
        await _menu_gift(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data == "u:account":
        await _menu_account(chat_id, msg_id, tg_user)
        await _answer_cb(cb_id, "")
    elif data == "u:referral":
        await _menu_referral(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data == "u:support":
        await _menu_support(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data == "u:links":
        await _menu_links(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data == "u:toggle":
        await _menu_toggle_service(chat_id, msg_id)
        await _answer_cb(cb_id, "")
    elif data == "u:del":
        await _menu_delete_service(chat_id, msg_id, tg_user)
        await _answer_cb(cb_id, "")
    elif data == "noop":
        await _answer_cb(cb_id, "")
    else:
        await _answer_cb(cb_id, "")


async def _handle_callback(cb):
    cb_id = cb.get("id")
    data = str(cb.get("data") or "")
    message = cb.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    msg_id = message.get("message_id")
    from_user = cb.get("from") or {}
    try:
        is_admin = _is_admin(from_user.get("id"))
    except Exception:
        is_admin = False
    if data.startswith("u:"):
        await _handle_u_callback(chat_id, cb_id, msg_id, data, from_user)
        return
    if data.startswith("adm:"):
        parts = data.split(":")
        if len(parts) >= 3 and parts[1] == "list":
            if not is_admin:
                await _answer_cb(cb_id, "⛔ دسترسی مدیریتی ندارید", alert=True)
                return
            await _cmd_approve(chat_id)
            await _answer_cb(cb_id, "")
            return
        rid = parts[1]
        act = parts[2]
        if not is_admin:
            await _answer_cb(cb_id, "⛔ دسترسی مدیریتی ندارید", alert=True)
            return
        if act == "ok":
            ok, msg = await _admin_approve(rid, from_user.get("id"))
            await _answer_cb(cb_id, msg, alert=ok)
        elif act == "no":
            ok, msg = await _admin_reject(rid, from_user.get("id"))
            await _answer_cb(cb_id, msg, alert=ok)
        else:
            await _answer_cb(cb_id, "")
        return
    if data.startswith("pg:"):
        if not is_admin:
            await _answer_cb(cb_id, "⛔ دسترسی مدیریتی ندارید", alert=True)
            return
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            page = 0
        LIST_PAGES[chat_id] = {"page": page}
        await _answer_cb(cb_id, "")
        await _cmd_users(chat_id, page)
    elif data == "noop":
        await _answer_cb(cb_id, "")
    elif data == "bot:restart":
        if not is_admin:
            await _answer_cb(cb_id, "⛔ دسترسی مدیریتی ندارید", alert=True)
            return
        await _answer_cb(cb_id, "🔄 در حال ریستارت ربات...")
        await restart_bot()
        await _send(chat_id, "✅ ربات مجدداً راه‌اندازی شد.")
    else:
        await _answer_cb(cb_id, "")


async def _handle_message(msg):
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    from_user = msg.get("from") or {}
    text = str(msg.get("text") or "").strip()
    is_admin = _is_admin(from_user.get("id"))

    # Receipt (photo / document / text) during the charge-receipt step
    if BOT_STATE.get(chat_id, {}).get("step") == "charge_receipt":
        photo = None
        for k in ("photo", "document"):
            arr = msg.get(k)
            if isinstance(arr, list) and arr:
                photo = arr[-1].get("file_id")
                break
        if photo or text:
            await _submit_receipt(chat_id, from_user, {"photo": photo or "", "text": text[:400] if not photo else ""})
            return

    # multi-step flow continuation (non-command text)
    cur = BOT_STATE.get(chat_id)
    if cur and not text.startswith("/"):
        await _continue_state(chat_id, from_user, cur, text)
        return

    low = text.lower()
    if low.startswith("/start"):
        await _cmd_start(chat_id, from_user, is_admin, text)
    elif text in ("🕹 منو", "/menu"):
        await _menu_home(chat_id)
    elif text in ("🛒 خرید", "/buy", "/shop"):
        await _menu_buy(chat_id)
    elif text in ("💳 کیف پول", "/wallet"):
        await _menu_wallet(chat_id)
    elif text in ("🎁 کد هدیه", "/gift"):
        await _menu_gift(chat_id)
    elif text in ("🎁 تست رایگان", "/trial"):
        await _cmd_trial(chat_id, from_user)
    elif text in ("🛟 پشتیبانی", "/support"):
        await _menu_support(chat_id)
    elif text in ("👤 حساب کاربری", "/account"):
        await _menu_account(chat_id, tg_user=from_user)
    elif text in ("📋 سرویس‌های من", "/me", "/status"):
        await _cmd_my_sub(chat_id)
    elif text in ("🎫 دریافت اشتراک", "/subscribe", "/new"):
        await _cmd_subscribe(chat_id, from_user, is_admin)
    elif text in ("🔗 لینک سابسکریپشن", "/sub", "/sublink"):
        uid, user = await _bound_user(chat_id)
        if not uid:
            await _send(chat_id, "⚠️ هنوز اشتراکی ندارید.\nاز «🕹 منو» شروع کنید.")
            return
        txt, btns = await _service_menu(chat_id)
        await _send(chat_id, txt, buttons=btns)
    elif text in ("🖼 کانفیگ و QR", "/qr"):
        await _cmd_qr(chat_id)
    elif text in ("❓ راهنما", "/help"):
        await _cmd_help(chat_id)
    elif low == "/cancel":
        await _cmd_cancel(chat_id)
    elif not is_admin:
        await _cmd_help(chat_id)
    elif text in ("📈 آمار", "/stats") or low == "/stats":
        await _cmd_stats(chat_id)
    elif text in ("➕ کاربر جدید",) or low == "/add":
        await _cmd_add_user(chat_id)
    elif low.startswith("/add "):
        await _cmd_add_direct(chat_id, text.split(" ", 1)[1])
    elif text in ("👥 لیست کاربران", "/users") or low == "/users":
        await _cmd_users(chat_id, 0)
    elif text in ("📢 پیام همگانی",) or low == "/broadcast":
        await _cmd_broadcast(chat_id)
    elif low.startswith("/broadcast "):
        await _cmd_broadcast_direct(chat_id, text.split(" ", 1)[1])
    elif low == "/gift":
        await _send(chat_id, "کاربرد: <code>/gift GB DAYS [COUNT]</code>")
    elif low.startswith("/gift "):
        await _cmd_gift(chat_id, text.split(" ", 1)[1])
    elif low == "/plans":
        await _cmd_plans(chat_id)
    elif low.startswith("/plan add "):
        await _cmd_plan_add(chat_id, text.split(" ", 2)[2])
    elif low.startswith("/plan del "):
        await _cmd_plan_del(chat_id, text.split(" ", 2)[2])
    elif low == "/plan":
        await _cmd_plans(chat_id)
    elif low == "/setcard":
        await _send(chat_id, "کاربرد: <code>/setcard 6037-XXXX شماره کارت صاحب حساب</code>")
    elif low.startswith("/setcard "):
        await _cmd_setcard(chat_id, text.split(" ", 1)[1])
    elif low == "/approve" or text == "/approve":
        await _cmd_approve(chat_id)
    elif low == "/toggle":
        await _send(chat_id, "کاربرد: <code>/toggle USERNAME</code>")
    elif low.startswith("/toggle "):
        await _cmd_toggle(chat_id, text.split(" ", 1)[1])
    elif low == "/reset":
        await _send(chat_id, "کاربرد: <code>/reset USERNAME</code>")
    elif low.startswith("/reset "):
        await _cmd_reset(chat_id, text.split(" ", 1)[1])
    elif low == "/extend":
        await _send(chat_id, "کاربرد: <code>/extend USERNAME DAYS</code>")
    elif low.startswith("/extend "):
        await _cmd_extend(chat_id, text.split(" ", 1)[1])
    elif low == "/del" or text in ("🔄 حذف/تغییر کاربر",):
        await _cmd_manager(chat_id, text)
    elif low.startswith("/del "):
        await _cmd_delete(chat_id, text.split(" ", 1)[1])
    elif text in ("⚙️ تنظیمات ربات", "/bot"):
        await _cmd_bot_settings(chat_id)
    else:
        await _cmd_manager(chat_id, text)


async def _handle_update(upd):
    if "callback_query" in upd:
        await _handle_callback(upd["callback_query"])
    elif "message" in upd:
        await _handle_message(upd["message"])


# ── Polling loop ────────────────────────────────────────────────────────────
async def _poll_loop():
    global _offset
    token = _token()
    if not token:
        logger.warning("Telegram bot: no token configured, polling not started")
        return
    logger.info("Telegram bot polling started")
    while not _stopped:
        try:
            params = {
                "timeout": 50,
                "offset": _offset,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
            resp = await P.http_client.get(
                TG_API.format(token=token, method="getUpdates"),
                params=params,
                # timeout config,
            )
            data = resp.json()
            if not data.get("ok"):
                desc = str(data.get("description") or "")
                if "409" in desc or "Conflict" in desc or "webhook" in desc.lower():
                    logger.error("Telegram bot conflict (webhook set?): %s", desc[:200])
                    await asyncio.sleep(10)
                else:
                    logger.warning("Telegram getUpdates error: %s", desc[:200])
                    await asyncio.sleep(5)
                continue
            for upd in data.get("result", []):
                try:
                    await _handle_update(upd)
                except Exception as exc:
                    logger.exception("telegram update handler error: %s", exc)
                _offset = max(int(upd.get("update_id") or 0) + 1, _offset)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telegram polling error: %s", str(exc)[:200])
            await asyncio.sleep(3)
    logger.info("Telegram bot polling stopped")


def start_bot() -> asyncio.Task | None:
    """Create the polling task if the bot is enabled. Returns the Task."""
    global POLL_TASK, _stopped
    cfg = _cfg()
    _stopped = False
    if not cfg.get("token"):
            if POLL_TASK and not POLL_TASK.done():
        return POLL_TASK
    POLL_TASK = asyncio.create_task(_poll_loop(), name="spider-telegram-bot")
    return POLL_TASK


async def stop_bot():
    global POLL_TASK, _stopped
    _stopped = True
    if POLL_TASK and not POLL_TASK.done():
        POLL_TASK.cancel()
        try:
            await POLL_TASK
        except (asyncio.CancelledError, Exception):
            pass
    POLL_TASK = None


async def restart_bot():
    await stop_bot()
    _stopped = False
    start_bot()


def bot_status() -> str:
    if POLL_TASK and not POLL_TASK.done():
        return "running"
    return "stopped"