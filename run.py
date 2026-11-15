"""Launcher for HONESTY AI VIP BOT.  Render start command:  python run.py

It loads main-4.py unchanged and only replaces the /start welcome (photo + 5 buttons).
ALL WELCOME TEXTS ARE IN THE "TEXTS" SECTION BELOW - edit them here.
The welcome photo is any .jpg / .jpeg / .png file uploaded next to this file in the repo.
"""
import importlib.util
import sys
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import CallbackQueryHandler

HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("bot_main", HERE / "main-4.py")
bot_main = importlib.util.module_from_spec(spec)
sys.modules["bot_main"] = bot_main  # needed by dataclasses inside main-4.py
spec.loader.exec_module(bot_main)

# =====================================================================
# TEXTS  (edit freely)
# =====================================================================
WELCOME_TEXT = """❤️‍🔥 Hello {first_name}

➡️ Welcome to FREE VIP BOT

🔥 Thanks for contacting us!

👇 SELECT YOUR OPTION BELOW 👇

📲 MESSAGE ADMIN: @honestytamil

🪩 PUBLIC GROUP:
https://t.me/honestyxz
"""

JOIN_TEXT = """❤️‍🔥 FREE VIP JOINING PROCESS

➡️ CREATE NEW ACCOUNT WITH THIS LINK

🔗 https://broker-qx.pro/sign-up/?lid=2416000

➡️ DEPOSIT AMOUNT MINIMUM 40$

➡️ AFTER DEPOSIT, SEND YOUR TRADER ID

🔥 START YOUR TRADING JOURNEY WITH US!

📈 TRADE RESPONSIBLY WITH PROPER MONEY MANAGEMENT.
"""

NEW_ACCOUNT_TEXT = """❤️‍🔥 HOW TO CREATE QUOTEX NEW ACCOUNT

➡️ 1. Click on my registration link:

🔗 https://broker-qx.pro/sign-up/?lid=2416000

➡️ 2. Select your Country.

➡️ 3. Enter your Email and create a strong Password.

➡️ 4. Accept Terms & Conditions.

➡️ 5. Click on Registration.

➡️ 6. Check your Email and verify your account using the verification link.

📈 DONE! YOUR ACCOUNT REGISTRATION PROCESS IS COMPLETE.

🔥 After registration, you can send your Trader ID to me
"""

GROUP_TEXT = """❤️‍🔥 WELCOME TO OUR OFFICIAL PUBLIC GROUP

🔥 Join Our Trading Community Today!

📢 GET DAILY UPDATES
📊 TRADING SIGNAL UPDATES
💬 COMMUNITY DISCUSSIONS
🚀 LATEST NEWS & ANNOUNCEMENTS

👇 CLICK BELOW TO JOIN OUR PUBLIC GROUP

🔗 https://t.me/honestyxz

❤️‍🔥 THANK YOU FOR BEING PART OF OUR COMMUNITY!

🔥 STAY CONNECTED & TRADE RESPONSIBLY!
"""

HELP_TEXT = """🔵 NEED HELP? WE ARE HERE FOR YOU! ❤️‍🔥

👋 Hello Trader!

❓ Do you have any questions or problems?

📲 CONTACT OUR SUPPORT:

👉 Telegram: @honestytamil

💬 Click the username below to message us directly:

🔗 https://t.me/honestytamil

❤️‍🔥 We are happy to help you!

🔥 THANK YOU FOR CONTACTING FREE VIP BOT.
"""

DELETE_TEXT = """🔴 HOW TO DELETE QUOTEX OLD ACCOUNT

➡️1. Go to Profile option.

➡️2. Scroll down and select Delete Account.

➡️3. Check your email - you will receive a link for deleting the account.

➡️4. Click on the link you received on email - your account is deleted successfully.

Now you can Register a new account using our link:
🔗https://broker-qx.pro/sign-up/?lid=2416000
"""

# button id -> (text, show the "Submit my Trader ID" button under it)
PAGES = {
    "join": (JOIN_TEXT, True),
    "new": (NEW_ACCOUNT_TEXT, True),
    "group": (GROUP_TEXT, False),
    "help": (HELP_TEXT, False),
    "old": (DELETE_TEXT, False),
}

# =====================================================================
# BUTTONS + HANDLERS  (no need to edit below)
# =====================================================================
WELCOME_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔵 FREE VIP JOINING PROCESS", callback_data="w:join")],
    [InlineKeyboardButton("🟢 HOW TO CREATE QUOTEX NEW ACCOUNT", callback_data="w:new")],
    [InlineKeyboardButton("🔵 I WANT TO JOIN PUBLIC GROUP", callback_data="w:group")],
    [InlineKeyboardButton("🔵 ASK ANY QUESTION OR PROBLEM", callback_data="w:help")],
    [InlineKeyboardButton("🔴 HOW TO DELETE QUOTEX OLD ACCOUNT", callback_data="w:old")],
])
SUBMIT_KB = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Submit my Trader ID", callback_data="submit")]])
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def _find_photo():
    files = sorted(p for p in HERE.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    return files[0] if files else None


async def welcome(update, context):
    user = update.effective_user
    text = WELCOME_TEXT.replace("{first_name}", user.first_name if user and user.first_name else "Trader")
    msg = update.effective_message
    photo = _find_photo()
    if photo:
        data = context.application.bot_data
        try:
            sent = await msg.reply_photo(photo=data.get("welcome_file_id") or photo.read_bytes(),
                                         caption=text, reply_markup=WELCOME_KB)
            data["welcome_file_id"] = sent.photo[-1].file_id  # reuse Telegram's copy next time
            return
        except Exception as exc:  # noqa: BLE001
            data.pop("welcome_file_id", None)
            bot_main.logger.warning("welcome photo failed: %s", type(exc).__name__)
    await msg.reply_text(text, reply_markup=WELCOME_KB, link_preview_options=NO_PREVIEW)


async def on_welcome_button(update, context):
    q = update.callback_query
    await q.answer()
    text, with_submit = PAGES[q.data.split(":", 1)[1]]
    await q.message.reply_text(text, reply_markup=SUBMIT_KB if with_submit else None,
                               link_preview_options=NO_PREVIEW)


_original_build = bot_main.build_application


def _build(settings, polling):
    app = _original_build(settings, polling)
    app.add_handler(CallbackQueryHandler(on_welcome_button, pattern=r"^w:(join|new|group|help|old)$"))
    return app


bot_main.start = welcome  # /start and /help now use the new welcome
bot_main.build_application = _build

if __name__ == "__main__":
    bot_main.main()
