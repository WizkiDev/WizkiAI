import discord
from discord.ext import commands, tasks
import requests
import asyncio
import subprocess
import sqlite3
import random
import datetime
import re

# ================= CONFIG =================
DISCORD_TOKEN = "YOUR_DISCORD_BOT_TOKEN_HERE"
OLLAMA_URL = "http://localhost:11434/api/generate"

MODELS = {
    "fast": "qwen2.5:3b",
    "smart": "qwen2.5:14b",
}
DEFAULT_MODEL_PREF = "smart"

ADMIN_IDS = {1423638809714884724}

DB_PATH = "bot_data.db"
MAX_MEMORY_TURNS = 10

PLANS = {
    "free": {"limit": 80, "price": 0},
    "plus": {"limit": 150, "price": 80},
    "pro": {"limit": 999999, "price": 250},
}

DEFAULT_PERSONALITY = "You are a helpful, friendly assistant in a Discord server."

# ================= BOT SETUP =================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ================= DATABASE =================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        coins INTEGER DEFAULT 50,
        plan TEXT DEFAULT 'free',
        messages_today INTEGER DEFAULT 0,
        last_reset TEXT,
        last_daily TEXT,
        last_work TEXT,
        last_beg TEXT,
        last_rob TEXT,
        model_pref TEXT DEFAULT 'smart',
        total_messages_ai INTEGER DEFAULT 0,
        games_played INTEGER DEFAULT 0,
        coins_won INTEGER DEFAULT 0,
        coins_lost INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS inventory (
        user_id TEXT,
        item_name TEXT,
        qty INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        channel_id TEXT,
        remind_at TEXT,
        message TEXT,
        done INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS memory (
        user_id TEXT,
        role TEXT,
        content TEXT,
        ts TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        command TEXT,
        output TEXT,
        ts TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO users (user_id, last_reset) VALUES (?, ?)",
                   (str(user_id), today_str()))
        conn.commit()
        c.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),))
        row = c.fetchone()
    conn.close()
    return dict(row)


def update_user(user_id, **fields):
    conn = db()
    c = conn.cursor()
    keys = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [str(user_id)]
    c.execute(f"UPDATE users SET {keys} WHERE user_id=?", values)
    conn.commit()
    conn.close()


def today_str():
    return datetime.date.today().isoformat()


def now_ts():
    return datetime.datetime.utcnow().isoformat()


def ensure_daily_reset(user):
    if user["last_reset"] != today_str():
        update_user(user["user_id"], messages_today=0, last_reset=today_str())
        user["messages_today"] = 0
        user["last_reset"] = today_str()
    return user


def get_setting(key, default=None):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=?", (key, value, value))
    conn.commit()
    conn.close()


def get_memory(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT role, content FROM memory WHERE user_id=? ORDER BY ts ASC", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_memory(user_id, role, content):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO memory (user_id, role, content, ts) VALUES (?, ?, ?, ?)",
              (str(user_id), role, content, now_ts()))
    # trim to last MAX_MEMORY_TURNS*2 rows (user+assistant pairs)
    c.execute("""DELETE FROM memory WHERE rowid IN (
        SELECT rowid FROM memory WHERE user_id=? ORDER BY ts ASC
        LIMIT MAX(0, (SELECT COUNT(*) FROM memory WHERE user_id=?) - ?)
    )""", (str(user_id), str(user_id), MAX_MEMORY_TURNS * 2))
    conn.commit()
    conn.close()


def clear_memory(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM memory WHERE user_id=?", (str(user_id),))
    conn.commit()
    conn.close()


def log_audit(user_id, command, output):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO audit_log (user_id, command, output, ts) VALUES (?, ?, ?, ?)",
              (str(user_id), command, output[:1500], now_ts()))
    conn.commit()
    conn.close()


init_db()


# ================= HELPERS =================
def is_admin_id(user_id):
    return user_id in ADMIN_IDS


def is_dm(ctx):
    return ctx.guild is None


def split_message(text, limit=2000):
    if not text:
        return ["(no output)"]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def query_ollama(prompt, model):
    response = requests.post(
        OLLAMA_URL,
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=180,
    )
    response.raise_for_status()
    return response.json()["response"].strip()


def run_shell(command, timeout=30):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        out = result.stdout.strip()
        err = result.stderr.strip()
        combined = out
        if err:
            combined += f"\n[stderr]\n{err}"
        combined += f"\n[exit code: {result.returncode}]"
        return combined.strip()
    except subprocess.TimeoutExpired:
        return "Command timed out."
    except Exception as e:
        return f"Error running command: {e}"


def web_search(query, max_results=5):
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        formatted = ""
        for r in results:
            formatted += f"- {r.get('title')}: {r.get('body')} ({r.get('href')})\n"
        return formatted
    except Exception as e:
        return f"Search error: {e}"


def build_prompt(user_id, new_message, personality):
    history = get_memory(user_id)
    convo = personality + "\n\n"
    for turn in history:
        role = "User" if turn["role"] == "user" else "Assistant"
        convo += f"{role}: {turn['content']}\n"
    convo += f"User: {new_message}\nAssistant:"
    return convo


async def do_ai_reply(ctx, user_id, question, use_web=False):
    user = get_user(user_id)
    user = ensure_daily_reset(user)
    limit = PLANS[user["plan"]]["limit"]

    if user["messages_today"] >= limit:
        await ctx.send(
            f"You've hit your daily limit ({limit} messages on the **{user['plan']}** plan). "
            f"Use `!plans` to see upgrades, or come back tomorrow."
        )
        return

    personality = get_setting("personality", DEFAULT_PERSONALITY)
    model = MODELS.get(user["model_pref"], MODELS[DEFAULT_MODEL_PREF])

    search_context = ""
    if use_web:
        async with ctx.typing():
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, web_search, question)
        search_context = f"\n\nHere are current web search results to help answer:\n{results}\n"

    prompt = build_prompt(user_id, question + search_context, personality)

    async with ctx.typing():
        loop = asyncio.get_event_loop()
        try:
            answer = await loop.run_in_executor(None, query_ollama, prompt, model)
        except Exception as e:
            await ctx.send(f"Error talking to the local model: {e}")
            return

    add_memory(user_id, "user", question)
    add_memory(user_id, "assistant", answer)
    update_user(user_id, messages_today=user["messages_today"] + 1,
                total_messages_ai=user["total_messages_ai"] + 1)

    for chunk in split_message(answer):
        await ctx.send(chunk)


# ================= EVENTS =================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if not reminder_checker.is_running():
        reminder_checker.start()
    if not daily_interest.is_running():
        daily_interest.start()


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # auto-reply on mention (skip if it was already a command)
    if bot.user in message.mentions and not message.content.startswith("!"):
        question = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if question:
            ctx = await bot.get_context(message)
            await do_ai_reply(ctx, message.author.id, question)


# ================= AI CHAT COMMANDS =================
@bot.command()
async def ask(ctx, *, question):
    await do_ai_reply(ctx, ctx.author.id, question)


@bot.command()
async def web(ctx, *, question):
    await do_ai_reply(ctx, ctx.author.id, question, use_web=True)


@bot.command()
async def reset(ctx):
    clear_memory(ctx.author.id)
    await ctx.send("Your conversation memory has been cleared.")


@bot.command(name="fast")
async def set_fast(ctx):
    update_user(ctx.author.id, model_pref="fast")
    await ctx.send(f"Switched to fast mode (`{MODELS['fast']}`).")


@bot.command(name="smart")
async def set_smart(ctx):
    update_user(ctx.author.id, model_pref="smart")
    await ctx.send(f"Switched to smart mode (`{MODELS['smart']}`).")


@bot.command()
async def setpersonality(ctx, *, text):
    if not is_admin_id(ctx.author.id):
        return
    set_setting("personality", text)
    await ctx.send("Personality updated.")


# ================= ECONOMY: INFO =================
@bot.command()
async def balance(ctx):
    user = get_user(ctx.author.id)
    user = ensure_daily_reset(user)
    limit = PLANS[user["plan"]]["limit"]
    limit_str = "unlimited" if limit >= 999999 else str(limit)
    await ctx.send(
        f"**{ctx.author.display_name}**\n"
        f"Coins: {user['coins']}\n"
        f"Plan: {user['plan']}\n"
        f"AI messages today: {user['messages_today']}/{limit_str}"
    )


@bot.command()
async def plans(ctx):
    lines = ["**Available plans:**"]
    for name, info in PLANS.items():
        limit_str = "unlimited" if info["limit"] >= 999999 else f"{info['limit']}/day"
        price_str = "free" if info["price"] == 0 else f"{info['price']} coins"
        lines.append(f"- **{name}** — {limit_str} AI messages — {price_str}")
    lines.append("\nBuy with `!buy <plan>`")
    await ctx.send("\n".join(lines))


@bot.command()
async def buy(ctx, plan_name: str):
    plan_name = plan_name.lower()
    if plan_name not in PLANS:
        await ctx.send("Unknown plan. Use `!plans` to see options.")
        return
    user = get_user(ctx.author.id)
    price = PLANS[plan_name]["price"]
    if user["plan"] == plan_name:
        await ctx.send(f"You already have the **{plan_name}** plan.")
        return
    if user["coins"] < price:
        await ctx.send(f"Not enough coins. You have {user['coins']}, need {price}.")
        return
    update_user(ctx.author.id, coins=user["coins"] - price, plan=plan_name)
    await ctx.send(f"Upgraded to **{plan_name}**! -{price} coins.")


@bot.command()
async def leaderboard(ctx):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()

    lines = ["**Top coin holders:**"]
    for i, row in enumerate(rows, 1):
        try:
            member = await ctx.guild.fetch_member(int(row["user_id"])) if ctx.guild else None
            name = member.display_name if member else f"User {row['user_id']}"
        except Exception:
            name = f"User {row['user_id']}"
        lines.append(f"{i}. {name} — {row['coins']} coins")
    await ctx.send("\n".join(lines))


# ================= ECONOMY: EARNING =================
def check_cooldown(user, field, cooldown_seconds):
    last = user.get(field)
    if not last:
        return True, 0
    last_dt = datetime.datetime.fromisoformat(last)
    elapsed = (datetime.datetime.utcnow() - last_dt).total_seconds()
    if elapsed >= cooldown_seconds:
        return True, 0
    return False, int(cooldown_seconds - elapsed)


@bot.command()
async def daily(ctx):
    user = get_user(ctx.author.id)
    ok, wait = check_cooldown(user, "last_daily", 24 * 3600)
    if not ok:
        hrs = wait // 3600
        mins = (wait % 3600) // 60
        await ctx.send(f"Already claimed. Try again in {hrs}h {mins}m.")
        return
    amount = random.randint(15, 40)
    update_user(ctx.author.id, coins=user["coins"] + amount, last_daily=now_ts())
    await ctx.send(f"You claimed your daily reward: +{amount} coins!")


@bot.command()
async def work(ctx):
    user = get_user(ctx.author.id)
    ok, wait = check_cooldown(user, "last_work", 30 * 60)
    if not ok:
        mins = wait // 60
        await ctx.send(f"You're tired. Rest {mins}m before working again.")
        return
    amount = random.randint(5, 20)
    update_user(ctx.author.id, coins=user["coins"] + amount, last_work=now_ts())
    jobs = ["delivered packages", "fixed a server", "walked some dogs", "sold lemonade", "mowed a lawn"]
    await ctx.send(f"You {random.choice(jobs)} and earned {amount} coins!")


@bot.command()
async def beg(ctx):
    user = get_user(ctx.author.id)
    ok, wait = check_cooldown(user, "last_beg", 15 * 60)
    if not ok:
        mins = wait // 60
        await ctx.send(f"People are tired of you. Wait {mins}m.")
        return
    if random.random() < 0.3:
        update_user(ctx.author.id, last_beg=now_ts())
        await ctx.send("Nobody gave you anything. 😔")
        return
    amount = random.randint(1, 8)
    update_user(ctx.author.id, coins=user["coins"] + amount, last_beg=now_ts())
    await ctx.send(f"A stranger felt bad for you and gave you {amount} coins.")


# ================= ECONOMY: GAMES =================
@bot.command()
async def coinflip(ctx, amount: int):
    user = get_user(ctx.author.id)
    if amount <= 0 or amount > user["coins"]:
        await ctx.send("Invalid bet amount.")
        return
    if random.random() < 0.5:
        update_user(ctx.author.id, coins=user["coins"] + amount)
        await ctx.send(f"🪙 Heads! You won {amount} coins.")
    else:
        update_user(ctx.author.id, coins=user["coins"] - amount)
        await ctx.send(f"🪙 Tails! You lost {amount} coins.")


@bot.command()
async def slots(ctx, amount: int):
    user = get_user(ctx.author.id)
    if amount <= 0 or amount > user["coins"]:
        await ctx.send("Invalid bet amount.")
        return

    symbols = ["🍒", "🍋", "🍇", "💎", "⭐"]
    spin = [random.choice(symbols) for _ in range(3)]
    display = " ".join(spin)

    if spin[0] == spin[1] == spin[2]:
        winnings = amount * 5
        update_user(ctx.author.id, coins=user["coins"] + winnings)
        await ctx.send(f"{display}\nJACKPOT! You won {winnings} coins!")
    elif len(set(spin)) == 2:
        winnings = amount
        update_user(ctx.author.id, coins=user["coins"] + winnings)
        await ctx.send(f"{display}\nSmall win! +{winnings} coins.")
    else:
        update_user(ctx.author.id, coins=user["coins"] - amount)
        await ctx.send(f"{display}\nNo match. -{amount} coins.")


@bot.command()
async def give(ctx, member: discord.Member, amount: int):
    if amount <= 0:
        await ctx.send("Invalid amount.")
        return
    sender = get_user(ctx.author.id)
    if sender["coins"] < amount:
        await ctx.send("You don't have that many coins.")
        return
    receiver = get_user(member.id)
    update_user(ctx.author.id, coins=sender["coins"] - amount)
    update_user(member.id, coins=receiver["coins"] + amount)
    await ctx.send(f"{ctx.author.display_name} gave {amount} coins to {member.display_name}.")


@bot.command()
async def rob(ctx, member: discord.Member):
    if member.id == ctx.author.id:
        await ctx.send("You can't rob yourself.")
        return
    robber = get_user(ctx.author.id)
    ok, wait = check_cooldown(robber, "last_rob", 60 * 60)
    if not ok:
        mins = wait // 60
        await ctx.send(f"Lay low for {mins}m before robbing again.")
        return

    target = get_user(member.id)
    if target["coins"] < 10:
        await ctx.send(f"{member.display_name} has nothing worth stealing.")
        update_user(ctx.author.id, last_rob=now_ts())
        return

    if random.random() < 0.5:
        stolen = random.randint(1, min(target["coins"], 50))
        update_user(ctx.author.id, coins=robber["coins"] + stolen, last_rob=now_ts())
        update_user(member.id, coins=target["coins"] - stolen)
        await ctx.send(f"You robbed {member.display_name} and got away with {stolen} coins!")
    else:
        penalty = min(robber["coins"], random.randint(5, 30))
        update_user(ctx.author.id, coins=robber["coins"] - penalty, last_rob=now_ts())
        await ctx.send(f"You got caught robbing {member.display_name} and paid a {penalty} coin fine.")


# ================= ADMIN: ECONOMY MANAGEMENT =================
@bot.command()
async def addcoins(ctx, member: discord.Member, amount: int):
    if not is_admin_id(ctx.author.id):
        return
    target = get_user(member.id)
    update_user(member.id, coins=target["coins"] + amount)
    await ctx.send(f"Gave {amount} coins to {member.display_name}.")


# ================= ADMIN: SYSTEM ACCESS =================
@bot.command()
async def sys(ctx, *, command):
    if not is_admin_id(ctx.author.id):
        return
    if not is_dm(ctx):
        await ctx.send("This command only works in DMs.")
        return
    async with ctx.typing():
        loop = asyncio.get_event_loop()
        output = await loop.run_in_executor(None, run_shell, command)
    log_audit(ctx.author.id, f"!sys {command}", output)
    for chunk in split_message(f"```\n{output}\n```"):
        await ctx.send(chunk)


AI_SYS_PROMPT = (
    "You are a system administration assistant. The user will describe what they "
    "want to do on a Linux server. Respond with ONLY the exact shell command to "
    "accomplish it, nothing else - no explanation, no markdown, no backticks."
)

pending_commands = {}


@bot.command()
async def aisys(ctx, *, request):
    if not is_admin_id(ctx.author.id):
        return
    if not is_dm(ctx):
        await ctx.send("This command only works in DMs.")
        return

    prompt = f"{AI_SYS_PROMPT}\n\nRequest: {request}\nCommand:"
    async with ctx.typing():
        loop = asyncio.get_event_loop()
        try:
            command = await loop.run_in_executor(None, query_ollama, prompt, MODELS["smart"])
        except Exception as e:
            await ctx.send(f"Error talking to the local model: {e}")
            return

    command = command.strip().strip("`").strip()
    confirm_msg = await ctx.send(
        f"Proposed command:\n```\n{command}\n```\nReact ✅ to run, ❌ to cancel. (30s)"
    )
    await confirm_msg.add_reaction("✅")
    await confirm_msg.add_reaction("❌")
    pending_commands[confirm_msg.id] = command

    def check(reaction, user):
        return (
            user.id == ctx.author.id
            and reaction.message.id == confirm_msg.id
            and str(reaction.emoji) in ("✅", "❌")
        )

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send("Timed out. Command not run.")
        pending_commands.pop(confirm_msg.id, None)
        return

    cmd = pending_commands.pop(confirm_msg.id, None)
    if str(reaction.emoji) == "❌" or cmd is None:
        await ctx.send("Cancelled.")
        return

    async with ctx.typing():
        loop = asyncio.get_event_loop()
        output = await loop.run_in_executor(None, run_shell, cmd)
    log_audit(ctx.author.id, f"!aisys {request} -> {cmd}", output)
    for chunk in split_message(f"```\n{output}\n```"):
        await ctx.send(chunk)


@bot.command()
async def status(ctx):
    if not is_admin_id(ctx.author.id):
        return
    loop = asyncio.get_event_loop()
    output = await loop.run_in_executor(
        None, run_shell, "echo CPU:; nproc; echo; free -h; echo; df -h /"
    )
    for chunk in split_message(f"```\n{output}\n```"):
        await ctx.send(chunk)


@bot.command()
async def auditlog(ctx, count: int = 10):
    if not is_admin_id(ctx.author.id):
        return
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (count,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await ctx.send("No audit log entries yet.")
        return
    lines = []
    for r in rows:
        lines.append(f"[{r['ts']}] user={r['user_id']} cmd={r['command']!r} -> {r['output'][:200]}")
    for chunk in split_message("\n".join(lines)):
        await ctx.send(f"```\n{chunk}\n```")


# ================= SHOP =================
SHOP_ITEMS = {
    "vip_badge": {"price": 150, "description": "Shows off VIP status (cosmetic)."},
    "lucky_charm": {"price": 200, "description": "A charm for good fortune (cosmetic)."},
    "trophy": {"price": 500, "description": "Bragging rights trophy (cosmetic)."},
}


def add_to_inventory(user_id, item_name):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT qty FROM inventory WHERE user_id=? AND item_name=?", (str(user_id), item_name))
    row = c.fetchone()
    if row:
        c.execute("UPDATE inventory SET qty=qty+1 WHERE user_id=? AND item_name=?", (str(user_id), item_name))
    else:
        c.execute("INSERT INTO inventory (user_id, item_name, qty) VALUES (?, ?, 1)", (str(user_id), item_name))
    conn.commit()
    conn.close()


@bot.command()
async def shop(ctx):
    lines = ["**Shop:**"]
    for name, info in SHOP_ITEMS.items():
        lines.append(f"- **{name}** — {info['price']} coins — {info['description']}")
    lines.append("\nBuy with `!buyitem <name>`")
    await ctx.send("\n".join(lines))


@bot.command()
async def buyitem(ctx, item_name: str):
    item_name = item_name.lower()
    if item_name not in SHOP_ITEMS:
        await ctx.send("Item not found. Check `!shop`.")
        return
    user = get_user(ctx.author.id)
    price = SHOP_ITEMS[item_name]["price"]
    if user["coins"] < price:
        await ctx.send(f"Not enough coins. You have {user['coins']}, need {price}.")
        return
    update_user(ctx.author.id, coins=user["coins"] - price)
    add_to_inventory(ctx.author.id, item_name)
    await ctx.send(f"Bought **{item_name}** for {price} coins!")


@bot.command()
async def inventory(ctx):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT item_name, qty FROM inventory WHERE user_id=?", (str(ctx.author.id),))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await ctx.send("Your inventory is empty. Check `!shop`.")
        return
    lines = [f"- {r['item_name']} x{r['qty']}" for r in rows]
    await ctx.send("**Your inventory:**\n" + "\n".join(lines))


# ================= MORE GAMES =================
def record_game_result(user_id, user, won_amount=0, lost_amount=0):
    update_user(
        user_id,
        games_played=user["games_played"] + 1,
        coins_won=user["coins_won"] + won_amount,
        coins_lost=user["coins_lost"] + lost_amount,
    )


def card_value():
    return random.randint(1, 11)  # simplified card draw


@bot.command()
async def blackjack(ctx, amount: int):
    user = get_user(ctx.author.id)
    if amount <= 0 or amount > user["coins"]:
        await ctx.send("Invalid bet amount.")
        return

    player = [card_value(), card_value()]
    dealer = [card_value(), card_value()]

    while sum(player) < 17:
        player.append(card_value())
        if sum(player) > 21:
            break

    while sum(dealer) < 17:
        dealer.append(card_value())
        if sum(dealer) > 21:
            break

    p_total, d_total = sum(player), sum(dealer)
    result = f"Your hand: {player} = {p_total}\nDealer hand: {dealer} = {d_total}\n"

    if p_total > 21:
        update_user(ctx.author.id, coins=user["coins"] - amount)
        record_game_result(ctx.author.id, user, lost_amount=amount)
        result += f"Bust! You lost {amount} coins."
    elif d_total > 21 or p_total > d_total:
        update_user(ctx.author.id, coins=user["coins"] + amount)
        record_game_result(ctx.author.id, user, won_amount=amount)
        result += f"You win! +{amount} coins."
    elif p_total == d_total:
        record_game_result(ctx.author.id, user)
        result += "Push. No coins changed."
    else:
        update_user(ctx.author.id, coins=user["coins"] - amount)
        record_game_result(ctx.author.id, user, lost_amount=amount)
        result += f"Dealer wins. -{amount} coins."

    await ctx.send(result)


@bot.command()
async def dice(ctx, member: discord.Member, amount: int):
    if member.id == ctx.author.id:
        await ctx.send("You can't duel yourself.")
        return
    challenger = get_user(ctx.author.id)
    opponent = get_user(member.id)
    if amount <= 0 or amount > challenger["coins"] or amount > opponent["coins"]:
        await ctx.send("Invalid bet — both players need enough coins.")
        return

    c_roll = random.randint(1, 6)
    o_roll = random.randint(1, 6)
    text = f"{ctx.author.display_name} rolled {c_roll}, {member.display_name} rolled {o_roll}.\n"

    if c_roll > o_roll:
        update_user(ctx.author.id, coins=challenger["coins"] + amount)
        update_user(member.id, coins=opponent["coins"] - amount)
        record_game_result(ctx.author.id, challenger, won_amount=amount)
        record_game_result(member.id, opponent, lost_amount=amount)
        text += f"{ctx.author.display_name} wins {amount} coins!"
    elif o_roll > c_roll:
        update_user(ctx.author.id, coins=challenger["coins"] - amount)
        update_user(member.id, coins=opponent["coins"] + amount)
        record_game_result(ctx.author.id, challenger, lost_amount=amount)
        record_game_result(member.id, opponent, won_amount=amount)
        text += f"{member.display_name} wins {amount} coins!"
    else:
        text += "Tie! No coins changed."

    await ctx.send(text)


# ================= MODERATION (admin only) =================
@bot.command()
async def kick(ctx, member: discord.Member, *, reason: str = "No reason given"):
    if not is_admin_id(ctx.author.id):
        return
    try:
        await member.kick(reason=reason)
        await ctx.send(f"Kicked {member.display_name}. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("I don't have permission to kick that member.")


@bot.command()
async def ban(ctx, member: discord.Member, *, reason: str = "No reason given"):
    if not is_admin_id(ctx.author.id):
        return
    try:
        await member.ban(reason=reason)
        await ctx.send(f"Banned {member.display_name}. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("I don't have permission to ban that member.")


@bot.command()
async def mute(ctx, member: discord.Member, minutes: int = 10, *, reason: str = "No reason given"):
    if not is_admin_id(ctx.author.id):
        return
    try:
        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        await member.timeout(until, reason=reason)
        await ctx.send(f"Muted {member.display_name} for {minutes}m. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("I don't have permission to timeout that member.")


@bot.command()
async def unmute(ctx, member: discord.Member):
    if not is_admin_id(ctx.author.id):
        return
    try:
        await member.timeout(None)
        await ctx.send(f"Unmuted {member.display_name}.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to do that.")


# ================= REMINDERS =================
DURATION_RE = re.compile(r"(\d+)\s*(s|sec|m|min|h|hour|d|day)s?", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400}


def parse_duration(text):
    total = 0
    for amount, unit in DURATION_RE.findall(text):
        total += int(amount) * UNIT_SECONDS[unit.lower()]
    return total


@bot.command()
async def remindme(ctx, duration: str, *, message: str):
    seconds = parse_duration(duration)
    if seconds <= 0:
        await ctx.send("Couldn't parse duration. Try things like `10m`, `2h`, `1d`.")
        return
    remind_at = (datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)).isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO reminders (user_id, channel_id, remind_at, message, done) VALUES (?, ?, ?, ?, 0)",
              (str(ctx.author.id), str(ctx.channel.id), remind_at, message))
    conn.commit()
    conn.close()
    await ctx.send(f"Got it — I'll remind you in {duration}.")


@tasks.loop(seconds=30)
async def reminder_checker():
    conn = db()
    c = conn.cursor()
    now = datetime.datetime.utcnow().isoformat()
    c.execute("SELECT * FROM reminders WHERE done=0 AND remind_at<=?", (now,))
    due = c.fetchall()
    for r in due:
        try:
            channel = bot.get_channel(int(r["channel_id"]))
            if channel:
                await channel.send(f"<@{r['user_id']}> reminder: {r['message']}")
        except Exception:
            pass
        c.execute("UPDATE reminders SET done=1 WHERE id=?", (r["id"],))
    conn.commit()
    conn.close()


# ================= STATS =================
@bot.command()
async def mystats(ctx):
    user = get_user(ctx.author.id)
    await ctx.send(
        f"**Stats for {ctx.author.display_name}:**\n"
        f"AI messages sent (all time): {user['total_messages_ai']}\n"
        f"Games played: {user['games_played']}\n"
        f"Coins won: {user['coins_won']}\n"
        f"Coins lost: {user['coins_lost']}\n"
        f"Current coins: {user['coins']}\n"
        f"Plan: {user['plan']}"
    )


@bot.command()
async def activity(ctx):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id, total_messages_ai FROM users ORDER BY total_messages_ai DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    lines = ["**Most active users (AI messages):**"]
    for i, row in enumerate(rows, 1):
        try:
            member = await ctx.guild.fetch_member(int(row["user_id"])) if ctx.guild else None
            name = member.display_name if member else f"User {row['user_id']}"
        except Exception:
            name = f"User {row['user_id']}"
        lines.append(f"{i}. {name} — {row['total_messages_ai']} messages")
    await ctx.send("\n".join(lines))


# ================= COIN INTEREST =================
INTEREST_RATE = 0.02  # 2% daily interest on saved coins
INTEREST_CAP = 5000    # only coins up to this amount earn interest, discourages hoarding exploits


@tasks.loop(hours=24)
async def daily_interest():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id, coins FROM users")
    rows = c.fetchall()
    for row in rows:
        eligible = min(row["coins"], INTEREST_CAP)
        gain = int(eligible * INTEREST_RATE)
        if gain > 0:
            c.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (gain, row["user_id"]))
    conn.commit()
    conn.close()
    print("Daily interest applied.")


@reminder_checker.before_loop
@daily_interest.before_loop
async def before_loops():
    await bot.wait_until_ready()


# ================= HELP =================
HELP_TEXT = {
    "AI Chat": [
        ("!ask <question>", "Chat with the AI"),
        ("!web <question>", "Chat with the AI, using live web search"),
        ("@mention the bot", "Auto-reply without a command"),
        ("!fast / !smart", "Switch your preferred model"),
        ("!reset", "Clear your AI memory"),
    ],
    "Economy - Info": [
        ("!balance", "Your coins, plan, and usage today"),
        ("!plans", "List available plans"),
        ("!buy <plan>", "Upgrade your plan with coins"),
        ("!leaderboard", "Top coin holders"),
    ],
    "Economy - Earning": [
        ("!daily", "Claim 15-40 coins, once per 24h"),
        ("!work", "Earn 5-20 coins, every 30 min"),
        ("!beg", "Small chance-based payout, every 15 min"),
    ],
    "Economy - Games": [
        ("!coinflip <amount>", "50/50 double or nothing"),
        ("!slots <amount>", "Slot machine"),
        ("!blackjack <amount>", "Play blackjack vs the dealer"),
        ("!dice @user <amount>", "1v1 dice duel"),
        ("!give @user <amount>", "Send coins to someone"),
        ("!rob @user", "Attempt to steal coins (risky)"),
    ],
    "Shop": [
        ("!shop", "View cosmetic items for sale"),
        ("!buyitem <name>", "Buy an item"),
        ("!inventory", "View what you own"),
    ],
    "Stats": [
        ("!mystats", "Your AI usage and game history"),
        ("!activity", "Server leaderboard by AI messages"),
    ],
    "Reminders": [
        ("!remindme <duration> <msg>", "e.g. !remindme 2h check the server"),
    ],
    "Admin only": [
        ("!setpersonality <text>", "Change the bot's personality"),
        ("!addcoins @user <amount>", "Grant coins"),
        ("!kick / !ban / !mute / !unmute @user", "Moderation"),
        ("!sys <cmd> (DM only)", "Run a raw shell command"),
        ("!aisys <request> (DM only)", "AI proposes a command, you confirm"),
        ("!status", "CPU/RAM/disk snapshot"),
        ("!auditlog [count]", "Recent admin command history"),
    ],
}


@bot.command(name="help")
async def help_command(ctx, *, category: str = None):
    if category:
        category_match = next((k for k in HELP_TEXT if k.lower() == category.lower()), None)
        if not category_match:
            await ctx.send(f"No category called '{category}'. Use `!help` to see all categories.")
            return
        lines = [f"**{category_match}**"]
        for cmd, desc in HELP_TEXT[category_match]:
            lines.append(f"`{cmd}` — {desc}")
        await ctx.send("\n".join(lines))
        return

    lines = ["**Commands** (use `!help <category>` for details)\n"]
    for cat, cmds in HELP_TEXT.items():
        preview = ", ".join(f"`{c.split()[0]}`" for c, _ in cmds)
        lines.append(f"**{cat}**: {preview}")
    for chunk in split_message("\n".join(lines)):
        await ctx.send(chunk)


bot.run(DISCORD_TOKEN)
