import os
import json
import sqlite3
import asyncio
import httpx
import time
import urllib.parse
from typing import Dict, Optional, Tuple, List
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load environment variables
load_dotenv()

# ==================== PROXY CONFIGURATION ====================
PROXY_HOST = os.getenv('PROXY_HOST', 'gw.dataimpulse.com')
PROXY_PORT = os.getenv('PROXY_PORT', '824')
PROXY_USER = os.getenv('PROXY_USER', '')
PROXY_PASS = os.getenv('PROXY_PASS', '')
PROXY_TYPE = os.getenv('PROXY_TYPE', 'http')  # http, socks5, socks4
PROXY_STATE = os.getenv('PROXY_STATE', '')

# Build proxy URL with authentication
def get_proxy_url():
    if not PROXY_USER or not PROXY_PASS:
        return None
    
    # Add state/location to username if specified
    username = PROXY_USER
    if PROXY_STATE:
        username = f"{PROXY_USER};state.{PROXY_STATE}"
    
    encoded_user = urllib.parse.quote(username)
    encoded_pass = urllib.parse.quote(PROXY_PASS)
    
    return f"{PROXY_TYPE}://{encoded_user}:{encoded_pass}@{PROXY_HOST}:{PROXY_PORT}"

PROXY_URL = get_proxy_url()

# ==================== IP CHECK SERVICE ====================
async def get_current_ip(client: httpx.AsyncClient) -> str:
    """Get current IP address from proxy"""
    try:
        # Multiple IP check services
        services = [
            "https://api.ipify.org?format=json",
            "https://httpbin.org/ip",
            "https://ipapi.co/json/"
        ]
        
        for service in services:
            try:
                response = await client.get(service, timeout=5.0)
                if response.status_code == 200:
                    data = response.json()
                    if 'ip' in data:
                        return data['ip']
                    elif 'origin' in data:
                        return data['origin']
            except:
                continue
        return "Unknown"
    except Exception as e:
        return f"Error: {str(e)[:30]}"

# ==================== DATABASE SETUP ====================
class Database:
    def __init__(self):
        db_path = os.getenv('DATABASE_PATH', 'data/cricway.db')
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                username TEXT PRIMARY KEY,
                password TEXT,
                user_id TEXT,
                auth_token TEXT,
                last_ip TEXT,
                is_active BOOLEAN DEFAULT 1,
                last_login TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS coupon_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                coupon_code TEXT,
                status TEXT,
                bonus REAL,
                balance_before REAL,
                balance_after REAL,
                proxy_ip TEXT,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                balance REAL,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_stats (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()
    
    def add_account(self, username: str, password: str, user_id: str = None, auth_token: str = None, last_ip: str = None) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO accounts (username, password, user_id, auth_token, last_ip, is_active, last_login) VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
                (username, password, user_id, auth_token, last_ip)
            )
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error adding account: {e}")
            return False
    
    def get_all_accounts(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT username, password, user_id, auth_token, last_ip, is_active FROM accounts WHERE is_active = 1")
        rows = cursor.fetchall()
        return [
            {
                "username": row[0],
                "password": row[1],
                "user_id": row[2],
                "auth_token": row[3],
                "last_ip": row[4],
                "is_active": bool(row[5])
            }
            for row in rows
        ]
    
    def update_account_token(self, username: str, auth_token: str, user_id: str, last_ip: str = None):
        cursor = self.conn.cursor()
        if last_ip:
            cursor.execute(
                "UPDATE accounts SET auth_token = ?, user_id = ?, last_ip = ?, last_login = CURRENT_TIMESTAMP WHERE username = ?",
                (auth_token, user_id, last_ip, username)
            )
        else:
            cursor.execute(
                "UPDATE accounts SET auth_token = ?, user_id = ?, last_login = CURRENT_TIMESTAMP WHERE username = ?",
                (auth_token, user_id, username)
            )
        self.conn.commit()
    
    def save_coupon_claim(self, username: str, coupon_code: str, status: str, bonus: float, balance_before: float, balance_after: float, proxy_ip: str = None):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO coupon_history (username, coupon_code, status, bonus, balance_before, balance_after, proxy_ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, coupon_code, status, bonus, balance_before, balance_after, proxy_ip)
        )
        self.conn.commit()
    
    def delete_account(self, username: str):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM accounts WHERE username = ?", (username,))
        self.conn.commit()
    
    def get_stats(self) -> Dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM accounts WHERE is_active = 1")
        total_accounts = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM coupon_history WHERE DATE(claimed_at) = DATE('now')")
        today_claims = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(bonus) FROM coupon_history WHERE DATE(claimed_at) = DATE('now') AND status = 'SUCCESS'")
        today_bonus = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*), SUM(bonus) FROM coupon_history WHERE status = 'SUCCESS'")
        total_claims, total_bonus = cursor.fetchone()
        return {
            'total_accounts': total_accounts,
            'today_claims': today_claims,
            'today_bonus': today_bonus,
            'total_claims': total_claims or 0,
            'total_bonus': total_bonus or 0
        }

# ==================== ASYNC API CLIENT ====================
class FastCricwayAccount:
    def __init__(self, username: str, password: str, auth_token: str = None, user_id: str = None):
        self.username = username
        self.password = password
        self.auth_token = auth_token
        self.user_id = user_id
        self.last_ip = None
        self.base_url = "https://api.uvwin2024.co"
        self.headers = {
            'Host': 'api.uvwin2024.co',
            'Connection': 'keep-alive',
            'accept': 'application/json, text/plain, */*',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
            'content-type': 'application/json',
            'sec-gpc': '1',
            'accept-language': 'en-US,en;q=0.9',
            'origin': 'https://www.cricway.io',
            'sec-fetch-site': 'cross-site',
            'sec-fetch-mode': 'cors',
            'sec-fetch-dest': 'empty',
            'referer': 'https://www.cricway.io/',
            'accept-encoding': 'gzip, deflate, br',
            'priority': 'u=1, i',
        }
        if self.auth_token:
            self.headers['authorization'] = self.auth_token
    
    async def async_login(self, client: httpx.AsyncClient = None) -> Tuple[bool, str]:
        json_data = {
            'username': self.username,
            'password': self.password,
            'otp': '',
            'loginRequestType': 'PHONE_SIGN_IN',
        }
        
        print(f"🔍 [DEBUG] Attempting login for {self.username}")
        
        login_headers = self.headers.copy()
        if 'authorization' in login_headers:
            del login_headers['authorization']
        
        async def do_login(client_to_use):
            response = await client_to_use.post(
                f'{self.base_url}/account/v2/login',
                headers=login_headers,
                json=json_data,
                timeout=15.0
            )
            return response

        try:
            if client:
                response = await do_login(client)
                # Get IP from proxy
                self.last_ip = await get_current_ip(client)
            else:
                async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL) as new_client:
                    response = await do_login(new_client)
                    self.last_ip = await get_current_ip(new_client)
            
            status = response.status_code
            response_text = response.text
                
            if status == 200:
                token = response_text.strip()
                if token.startswith('eyJ'):
                    self.auth_token = token
                    self.headers['authorization'] = self.auth_token
                    
                    import base64
                    token_parts = token.split('.')
                    if len(token_parts) > 1:
                        payload = token_parts[1]
                        payload += '=' * (4 - len(payload) % 4)
                        decoded = base64.b64decode(payload).decode('utf-8')
                        token_data = json.loads(decoded)
                        self.user_id = str(token_data.get('uid', token_data.get('userId', '')))
                    
                    print(f"✅ [DEBUG] Login success for {self.username} (IP: {self.last_ip})")
                    return True, "Login successful"
            
            print(f"❌ [DEBUG] Login failed for {self.username}: HTTP {status}")
            if status == 403:
                print(f"⚠️ [WARNING] Blocked! IP: {self.last_ip}")
            return False, f"HTTP {status}"
            
        except Exception as e:
            print(f"❌ [DEBUG] Login error for {self.username}: {str(e)}")
            return False, str(e)
    
    async def async_get_balance(self, client: httpx.AsyncClient) -> Tuple[bool, float]:
        if not self.auth_token:
            return False, 0
        
        try:
            response = await client.get(
                f'{self.base_url}/wallet/v2/wallets/{self.user_id}/balance',
                headers=self.headers,
                timeout=10.0
            )
            if response.status_code == 200:
                data = response.json()
                return True, float(data.get('balance', 0))
            return False, 0
        except:
            return False, 0
    
    async def async_claim_coupon(self, client: httpx.AsyncClient, coupon_code: str) -> Tuple[bool, str, float]:
        if not self.auth_token:
            return False, "Not authenticated", 0
        
        params = {'coupon_code': coupon_code}
        
        try:
            response = await client.get(
                f'{self.base_url}/marketing/v1/bonuses/special-bonus',
                headers=self.headers, 
                params=params,
                timeout=15.0
            )
            
            status = response.status_code
            response_text = response.text
            
            if status == 200:
                try:
                    data = json.loads(response_text)
                    bonus = data.get('data', {}).get('amount', 0)
                    return True, "Success", float(bonus)
                except:
                    return True, "Claimed", 0
            elif status == 409:
                return False, "Limit exhausted", 0
            elif status == 401:
                return False, "Unauthorized", 0
            else:
                return False, f"HTTP {status}", 0
        except Exception as e:
            return False, str(e), 0

# ==================== TELEGRAM BOT ====================
class CricwayBot:
    def __init__(self):
        self.db = Database()
        self.accounts = []
        self.load_accounts()
    
    def load_accounts(self):
        accounts_data = self.db.get_all_accounts()
        self.accounts = [
            FastCricwayAccount(
                username=acc['username'],
                password=acc['password'],
                auth_token=acc['auth_token'],
                user_id=acc['user_id']
            )
            for acc in accounts_data
        ]
        print(f"✅ Loaded {len(self.accounts)} accounts")
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = self.db.get_stats()
        
        # Show proxy status
        proxy_status = "✅ Active" if PROXY_URL else "❌ Not Configured"
        proxy_ip = "Checking..."
        
        try:
            async with httpx.AsyncClient(proxy=PROXY_URL, verify=False) as client:
                proxy_ip = await get_current_ip(client)
        except:
            proxy_ip = "Failed to fetch"
        
        welcome_msg = f"""
🚀 *CRICWAY PROXY BOT* 🚀

*Proxy Status:*
🔐 Proxy: {proxy_status}
🌍 Proxy IP: `{proxy_ip}`
📍 Location: {PROXY_STATE or 'Auto'}

*Bot Stats:*
👥 Accounts: {stats['total_accounts']}
📊 Today Claims: {stats['today_claims']}
💰 Today Bonus: ₹{stats['today_bonus']:.2f}
💎 Total Bonus: ₹{stats['total_bonus']:.2f}

*Commands:*
🔐 `/add username password` - Add account
🔄 `/loginall` - Login all accounts
🎫 `/claim CODE` - Claim coupon
💰 `/balance` - Check balances
✅ `/check` - Check login status
📊 `/stats` - Statistics
🌍 `/ip` - Show proxy IP
❌ `/remove username` - Remove account
        """
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    
    async def show_ip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current proxy IP"""
        await update.message.reply_text("🌍 Checking proxy IP...")
        
        try:
            async with httpx.AsyncClient(proxy=PROXY_URL, verify=False) as client:
                ip = await get_current_ip(client)
                
                # Get detailed location info
                try:
                    response = await client.get("https://ipapi.co/json/", timeout=5.0)
                    if response.status_code == 200:
                        details = response.json()
                        location_msg = f"""
🌍 *Proxy Information*

📡 *IP Address:* `{ip}`
📍 *Country:* {details.get('country_name', 'Unknown')}
🏙️ *City:* {details.get('city', 'Unknown')}
🗺️ *Region:* {details.get('region', 'Unknown')}
📮 *Postal:* {details.get('postal', 'Unknown')}
📱 *ISP:* {details.get('org', 'Unknown')}
🔌 *Proxy Type:* {PROXY_TYPE.upper()}

✅ *Proxy is working correctly!*
                        """
                    else:
                        location_msg = f"🌍 Proxy IP: `{ip}`"
                except:
                    location_msg = f"🌍 Proxy IP: `{ip}`"
                
                await update.message.reply_text(location_msg, parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to get proxy IP: {str(e)}")
    
    async def add_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: `/add username password`", parse_mode='Markdown')
            return
        
        username, password = args[0], args[1]
        
        await update.message.reply_text(f"🔐 Verifying account *{username}* via proxy...", parse_mode='Markdown')
        
        account = FastCricwayAccount(username, password)
        
        try:
            async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL) as client:
                # Get proxy IP first
                proxy_ip = await get_current_ip(client)
                success, msg = await account.async_login(client)
            
            if success:
                self.db.add_account(username, password, account.user_id, account.auth_token, proxy_ip)
                self.load_accounts()
                await update.message.reply_text(
                    f"✅ Account *{username}* added!\n"
                    f"🆔 ID: {account.user_id}\n"
                    f"🌍 Login IP: `{proxy_ip}`",
                    parse_mode='Markdown'
                )
            else:
                error_msg = f"❌ Failed: {msg}"
                await update.message.reply_text(error_msg, parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
    
    async def login_all_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text(f"🔄 Re-logging {len(self.accounts)} accounts via proxy...")
        start_time = time.time()
        
        async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL) as client:
            # Get proxy IP first
            proxy_ip = await get_current_ip(client)
            
            tasks = [acc.async_login(client) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        success_count = 0
        for acc, (success, msg) in zip(self.accounts, results):
            if success:
                self.db.update_account_token(acc.username, acc.auth_token, acc.user_id, proxy_ip)
                success_count += 1
        
        elapsed = time.time() - start_time
        
        result_msg = f"✅ *Login Complete!*\n"
        result_msg += f"🌍 Proxy IP: `{proxy_ip}`\n"
        result_msg += f"⏱️ Time: {elapsed:.2f}s\n"
        result_msg += f"📊 Success: {success_count}/{len(self.accounts)}"
        
        await update.message.reply_text(result_msg, parse_mode='Markdown')
    
    async def claim_coupon(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/claim COUPON_CODE`", parse_mode='Markdown')
            return
        
        coupon_code = args[0].upper()
        
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text(f"⚡ Claiming *{coupon_code}* for {len(self.accounts)} accounts via proxy...")
        start_time = time.time()
        
        async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL) as client:
            proxy_ip = await get_current_ip(client)
            
            # Get balances before
            balance_tasks = [acc.async_get_balance(client) for acc in self.accounts]
            balances_before = await asyncio.gather(*balance_tasks)
            
            # Claim coupons
            claim_tasks = [acc.async_claim_coupon(client, coupon_code) for acc in self.accounts]
            claim_results = await asyncio.gather(*claim_tasks)
            
            # Get balances after
            balance_after_tasks = [acc.async_get_balance(client) for acc in self.accounts]
            balances_after = await asyncio.gather(*balance_after_tasks)
        
        elapsed = time.time() - start_time
        
        success_count = sum(1 for r in claim_results if r[0])
        total_bonus = sum(r[2] for r in claim_results if r[0])
        
        # Save to database with proxy IP
        for i, acc in enumerate(self.accounts):
            if claim_results[i][0]:
                self.db.save_coupon_claim(
                    acc.username, coupon_code, "SUCCESS",
                    claim_results[i][2],
                    balances_before[i][1] if balances_before[i][0] else 0,
                    balances_after[i][1] if balances_after[i][0] else 0,
                    proxy_ip
                )
        
        result_msg = f"🎫 *{coupon_code}*\n"
        result_msg += f"🌍 Proxy IP: `{proxy_ip}`\n"
        result_msg += f"⚡ Time: {elapsed:.2f}s\n"
        result_msg += f"📊 Success: {success_count}/{len(self.accounts)}\n"
        result_msg += f"💰 Total Bonus: ₹{total_bonus:.2f}"
        
        await update.message.reply_text(result_msg, parse_mode='Markdown')
    
    async def check_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text("💰 Fetching balances via proxy...")
        
        async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL) as client:
            tasks = [acc.async_get_balance(client) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        balance_msg = "💰 *Balances*\n\n"
        total = 0
        
        for acc, (success, balance) in zip(self.accounts, results):
            if success:
                balance_msg += f"✅ *{acc.username}*: ₹{balance:.2f}\n"
                total += balance
            else:
                balance_msg += f"❌ *{acc.username}*: Failed\n"
        
        balance_msg += f"\n📊 *Total*: ₹{total:.2f}"
        await update.message.reply_text(balance_msg, parse_mode='Markdown')
    
    async def check_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text("🔍 Checking login status via proxy...")
        
        async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL) as client:
            tasks = [acc.async_get_balance(client) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        status_msg = "✅ *Account Status*\n\n"
        working = 0
        
        for acc, (success, _) in zip(self.accounts, results):
            if success:
                status_msg += f"✅ *{acc.username}*: Online\n"
                working += 1
            else:
                status_msg += f"❌ *{acc.username}*: Offline\n"
        
        status_msg += f"\n📊 Online: {working}/{len(self.accounts)}"
        await update.message.reply_text(status_msg, parse_mode='Markdown')
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = self.db.get_stats()
        
        stats_msg = f"📊 *Bot Statistics*\n\n"
        stats_msg += f"👥 Total Accounts: {stats['total_accounts']}\n"
        stats_msg += f"📝 Today's Claims: {stats['today_claims']}\n"
        stats_msg += f"💰 Today's Bonus: ₹{stats['today_bonus']:.2f}\n"
        stats_msg += f"📊 Total Claims: {stats['total_claims']}\n"
        stats_msg += f"💎 Total Bonus: ₹{stats['total_bonus']:.2f}"
        
        await update.message.reply_text(stats_msg, parse_mode='Markdown')
    
    async def remove_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/remove username`", parse_mode='Markdown')
            return
        
        username = args[0]
        self.db.delete_account(username)
        self.load_accounts()
        await update.message.reply_text(f"✅ Account *{username}* removed!", parse_mode='Markdown')

# ==================== MAIN ====================
def main():
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not found!")
        return
    
    # Print proxy configuration on startup
    print("=" * 50)
    print("🚀 CRICWAY PROXY BOT")
    print("=" * 50)
    if PROXY_URL:
        print(f"✅ Proxy: {PROXY_TYPE.upper()}://{PROXY_HOST}:{PROXY_PORT}")
        print(f"📍 Location: {PROXY_STATE or 'Auto'}")
        print(f"👤 Username: {PROXY_USER[:20]}...")
    else:
        print("⚠️ No proxy configured!")
    print("=" * 50)
    
    bot = CricwayBot()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("add", bot.add_account))
    app.add_handler(CommandHandler("loginall", bot.login_all_accounts))
    app.add_handler(CommandHandler("claim", bot.claim_coupon))
    app.add_handler(CommandHandler("balance", bot.check_balance))
    app.add_handler(CommandHandler("check", bot.check_status))
    app.add_handler(CommandHandler("stats", bot.stats))
    app.add_handler(CommandHandler("remove", bot.remove_account))
    app.add_handler(CommandHandler("ip", bot.show_ip))  # New command to show proxy IP
    
    print("🚀 Bot is starting...")
    print(f"📊 Loaded {len(bot.accounts)} accounts")
    print("✅ Bot is ready!")
    
    app.run_polling()

if __name__ == "__main__":
    main()
