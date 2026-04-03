import os
import json
import sqlite3
import asyncio
import httpx
import time
import base64
from typing import Dict, Optional, Tuple, List
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load environment variables
load_dotenv()

# ==================== DATABASE SETUP ====================
class Database:
    def __init__(self):
        # Use Railway's persistent storage or local
        db_path = os.getenv('DATABASE_PATH', 'data/cricway.db')
        
        # Create directory if it doesn't exist
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
    
    def add_account(self, username: str, password: str, user_id: str = None, auth_token: str = None) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO accounts (username, password, user_id, auth_token, is_active, last_login) VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
                (username, password, user_id, auth_token)
            )
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error adding account: {e}")
            return False
    
    def get_all_accounts(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT username, password, user_id, auth_token, is_active FROM accounts WHERE is_active = 1")
        rows = cursor.fetchall()
        return [
            {
                "username": row[0],
                "password": row[1],
                "user_id": row[2],
                "auth_token": row[3],
                "is_active": bool(row[4])
            }
            for row in rows
        ]
    
    def update_account_token(self, username: str, auth_token: str, user_id: str):
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE accounts SET auth_token = ?, user_id = ?, last_login = CURRENT_TIMESTAMP WHERE username = ?",
            (auth_token, user_id, username)
        )
        self.conn.commit()
    
    def deactivate_account(self, username: str):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE accounts SET is_active = 0 WHERE username = ?", (username,))
        self.conn.commit()
    
    def save_coupon_claim(self, username: str, coupon_code: str, status: str, bonus: float, balance_before: float, balance_after: float):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO coupon_history (username, coupon_code, status, bonus, balance_before, balance_after) VALUES (?, ?, ?, ?, ?, ?)",
            (username, coupon_code, status, bonus, balance_before, balance_after)
        )
        self.conn.commit()
    
    def save_balance(self, username: str, balance: float):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO balance_history (username, balance) VALUES (?, ?)",
            (username, balance)
        )
        self.conn.commit()
    
    def delete_account(self, username: str):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM accounts WHERE username = ?", (username,))
        self.conn.commit()
    
    def get_stats(self) -> Dict:
        cursor = self.conn.cursor()
        
        # Total accounts
        cursor.execute("SELECT COUNT(*) FROM accounts WHERE is_active = 1")
        total_accounts = cursor.fetchone()[0]
        
        # Total claims today
        cursor.execute("SELECT COUNT(*) FROM coupon_history WHERE DATE(claimed_at) = DATE('now')")
        today_claims = cursor.fetchone()[0]
        
        # Total bonus today
        cursor.execute("SELECT SUM(bonus) FROM coupon_history WHERE DATE(claimed_at) = DATE('now') AND status = 'SUCCESS'")
        today_bonus = cursor.fetchone()[0] or 0
        
        # Total all time
        cursor.execute("SELECT COUNT(*), SUM(bonus) FROM coupon_history WHERE status = 'SUCCESS'")
        total_claims, total_bonus = cursor.fetchone()
        total_claims = total_claims or 0
        total_bonus = total_bonus or 0
        
        return {
            'total_accounts': total_accounts,
            'today_claims': today_claims,
            'today_bonus': today_bonus,
            'total_claims': total_claims,
            'total_bonus': total_bonus
        }

# ==================== ASYNC API CLIENT ====================
class FastCricwayAccount:
    def __init__(self, username: str, password: str, auth_token: str = None, user_id: str = None):
        self.username = username
        self.password = password
        self.auth_token = auth_token
        self.user_id = user_id
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
        """Async login using httpx for better Cloudflare bypass on Railway"""
        json_data = {
            'username': self.username,
            'password': self.password,
            'otp': '',
            'loginRequestType': 'PHONE_SIGN_IN',
        }
        
        print(f"🔍 [DEBUG] Attempting login for {self.username}")
        
        # Fresh login headers (no old auth token)
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
            else:
                # Try using HTTP/2 first for better Cloudflare bypass
                try:
                    async with httpx.AsyncClient(http2=True, verify=False) as new_client:
                        response = await do_login(new_client)
                except (ImportError, RuntimeError, TypeError) as h2_err:
                    # Fallback to HTTP/1.1 if h2 is not installed
                    if "http2" in str(h2_err).lower() or "h2" in str(h2_err).lower():
                        print(f"⚠️ [WARNING] HTTP/2 not available, falling back to HTTP/1.1: {h2_err}")
                        async with httpx.AsyncClient(http2=False, verify=False) as new_client:
                            response = await do_login(new_client)
                    else:
                        raise h2_err
            
            status = response.status_code
            response_text = response.text
            
            if status == 200:
                token = response_text.strip()
                if token.startswith('eyJ'):
                    self.auth_token = token
                    self.headers['authorization'] = self.auth_token
                    
                    # Extract user_id from token
                    token_parts = token.split('.')
                    if len(token_parts) > 1:
                        payload = token_parts[1]
                        payload += '=' * (4 - len(payload) % 4)
                        decoded = base64.b64decode(payload).decode('utf-8')
                        token_data = json.loads(decoded)
                        self.user_id = str(token_data.get('uid', token_data.get('userId', '')))
                    
                    print(f"✅ [DEBUG] Login success for {self.username}")
                    return True, "Login successful"
            
            print(f"❌ [DEBUG] Login failed for {self.username}: HTTP {status}")
            if status == 403:
                print(f"⚠️ [WARNING] Cloudflare Blocked! Response: {response_text[:200]}")
            return False, f"HTTP {status}"
            
        except Exception as e:
            print(f"❌ [DEBUG] Login error for {self.username}: {str(e)}")
            return False, str(e)
    
    async def async_get_balance(self, client: httpx.AsyncClient) -> Tuple[bool, float]:
        """Async get balance using httpx"""
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
        except Exception as e:
            print(f"Balance error for {self.username}: {e}")
            return False, 0
    
    async def async_claim_coupon(self, client: httpx.AsyncClient, coupon_code: str) -> Tuple[bool, str, float]:
        """Async claim coupon using httpx"""
        if not self.auth_token:
            return False, "Not authenticated", 0
        
        params = {'coupon_code': coupon_code}
        
        print(f"🔍 [DEBUG] Claiming coupon {coupon_code} for {self.username}")
        
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
                    print(f"✅ [DEBUG] Claim success for {self.username}: ₹{bonus}")
                    return True, "Success", float(bonus)
                except:
                    print(f"✅ [DEBUG] Claim success (Claimed) for {self.username}")
                    return True, "Claimed", 0
            else:
                try:
                    data = json.loads(response_text)
                    api_msg = data.get('message', response_text[:100])
                except:
                    api_msg = response_text[:100]
                
                if status == 409:
                    print(f"❌ [DEBUG] Claim failed for {self.username}: Limit exhausted")
                    return False, "Limit exhausted", 0
                elif status == 401:
                    print(f"❌ [DEBUG] Claim failed for {self.username}: Unauthorized")
                    return False, "Unauthorized", 0
                else:
                    print(f"❌ [DEBUG] Claim failed for {self.username}: HTTP {status}")
                    return False, api_msg, 0
                    
        except Exception as e:
            print(f"❌ [DEBUG] Claim error for {self.username}: {str(e)}")
            return False, str(e), 0

# ==================== TELEGRAM BOT ====================
class CricwayBot:
    def __init__(self):
        self.db = Database()
        self.accounts = []
        self.load_accounts()
    
    def load_accounts(self):
        """Load all accounts from database"""
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
        """Start command"""
        stats = self.db.get_stats()
        
        welcome_msg = f"""
🚀 *CRICWAY ULTRA-FAST BOT* 🚀

*Bot Status:*
👥 Accounts: {stats['total_accounts']}
📊 Today Claims: {stats['today_claims']}
💰 Today Bonus: ₹{stats['today_bonus']:.2f}
💎 Total Bonus: ₹{stats['total_bonus']:.2f}

*Commands:*
🔐 `/add username password` - Add new account
🔄 `/loginall` - Force fresh login for ALL accounts
🎫 `/claim CODE` - Claim coupon (ULTRA FAST!)
💰 `/balance` - Check all balances
✅ `/check` - Check login status
📊 `/stats` - View statistics
❌ `/remove username` - Remove account
🔁 `/relogin username [pass]` - Force login for specific account

*Speed:* ⚡ 3-5 seconds for 50 accounts!
        """
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    
    async def add_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add new account"""
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: `/add username password`", parse_mode='Markdown')
            return
        
        username, password = args[0], args[1]
        
        await update.message.reply_text(f"🔐 Verifying account *{username}*...", parse_mode='Markdown')
        
        # Verify account by logging in
        account = FastCricwayAccount(username, password)
        try:
            async with httpx.AsyncClient(http2=True, verify=False) as client:
                success, msg = await account.async_login(client)
        except Exception as e:
            success, msg = False, str(e)
        
        if success:
            self.db.add_account(username, password, account.user_id, account.auth_token)
            self.load_accounts()
            await update.message.reply_text(
                f"✅ Account *{username}* added successfully!\n🆔 User ID: {account.user_id}",
                parse_mode='Markdown'
            )
        else:
            error_msg = f"❌ Failed to add *{username}*: {msg}"
            await update.message.reply_text(error_msg, parse_mode='Markdown')
    
    async def login_all_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Auto re-login ALL accounts"""
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found! Use /add first.")
            return
        
        await update.message.reply_text(f"🔄 Re-logging {len(self.accounts)} accounts...")
        start_time = time.time()
        
        try:
            async with httpx.AsyncClient(http2=True, verify=False) as client:
                tasks = [acc.async_login(client) for acc in self.accounts]
                results = await asyncio.gather(*tasks)
        except Exception as e:
            await update.message.reply_text(f"❌ Login failed: {str(e)}")
            return
        
        success_count = sum(1 for r in results if r[0])
        elapsed = time.time() - start_time
        
        result_msg = f"✅ *Login Complete!*\n⏱️ Time: {elapsed:.2f}s\n📊 Success: {success_count}/{len(self.accounts)}"
        await update.message.reply_text(result_msg, parse_mode='Markdown')
    
    async def claim_coupon(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Claim coupon for ALL accounts"""
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/claim COUPON_CODE`", parse_mode='Markdown')
            return
        
        coupon_code = args[0].upper()
        
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text(f"⚡ Claiming *{coupon_code}* for {len(self.accounts)} accounts...")
        start_time = time.time()
        
        try:
            async with httpx.AsyncClient(http2=True, verify=False) as client:
                # Get balances before
                balance_tasks = [acc.async_get_balance(client) for acc in self.accounts]
                balances_before = await asyncio.gather(*balance_tasks)
                
                # Claim coupons
                claim_tasks = [acc.async_claim_coupon(client, coupon_code) for acc in self.accounts]
                claim_results = await asyncio.gather(*claim_tasks)
                
                # Get balances after
                balance_after_tasks = [acc.async_get_balance(client) for acc in self.accounts]
                balances_after = await asyncio.gather(*balance_after_tasks)
        except Exception as e:
            await update.message.reply_text(f"❌ Claim failed: {str(e)}")
            return
        
        elapsed = time.time() - start_time
        
        success_count = sum(1 for r in claim_results if r[0])
        total_bonus = sum(r[2] for r in claim_results if r[0])
        
        result_msg = f"🎫 *{coupon_code}*\n⚡ Time: {elapsed:.2f}s\n📊 Success: {success_count}/{len(self.accounts)}\n💰 Total Bonus: ₹{total_bonus:.2f}"
        await update.message.reply_text(result_msg, parse_mode='Markdown')
    
    async def check_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check all balances"""
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text("💰 Fetching balances...")
        
        try:
            async with httpx.AsyncClient(http2=True, verify=False) as client:
                tasks = [acc.async_get_balance(client) for acc in self.accounts]
                results = await asyncio.gather(*tasks)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed: {str(e)}")
            return
        
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
        """Check login status"""
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text("🔍 Checking status...")
        
        try:
            async with httpx.AsyncClient(http2=True, verify=False) as client:
                tasks = [acc.async_get_balance(client) for acc in self.accounts]
                results = await asyncio.gather(*tasks)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed: {str(e)}")
            return
        
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
    
    async def relogin_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Re-login specific account"""
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/relogin username`", parse_mode='Markdown')
            return
        
        username = args[0]
        
        account = next((acc for acc in self.accounts if acc.username == username), None)
        if not account:
            await update.message.reply_text(f"❌ Account *{username}* not found!", parse_mode='Markdown')
            return
        
        await update.message.reply_text(f"🔄 Re-logging *{username}*...", parse_mode='Markdown')
        
        try:
            async with httpx.AsyncClient(http2=True, verify=False) as client:
                success, msg = await account.async_login(client)
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
            return
        
        if success:
            self.db.update_account_token(username, account.auth_token, account.user_id)
            await update.message.reply_text(f"✅ *{username}* re-logged successfully!", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Failed: {msg}", parse_mode='Markdown')
    
    async def remove_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove an account"""
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/remove username`", parse_mode='Markdown')
            return
        
        username = args[0]
        self.db.delete_account(username)
        self.load_accounts()
        await update.message.reply_text(f"✅ Account *{username}* removed!", parse_mode='Markdown')
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show statistics"""
        stats = self.db.get_stats()
        
        stats_msg = f"📊 *Bot Statistics*\n\n"
        stats_msg += f"👥 Total Accounts: {stats['total_accounts']}\n"
        stats_msg += f"📝 Today's Claims: {stats['today_claims']}\n"
        stats_msg += f"💰 Today's Bonus: ₹{stats['today_bonus']:.2f}\n"
        stats_msg += f"📊 Total Claims: {stats['total_claims']}\n"
        stats_msg += f"💎 Total Bonus: ₹{stats['total_bonus']:.2f}"
        
        await update.message.reply_text(stats_msg, parse_mode='Markdown')

# ==================== MAIN ====================
def main():
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not found!")
        print("Please set BOT_TOKEN in environment variables")
        return
    
    bot = CricwayBot()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("add", bot.add_account))
    app.add_handler(CommandHandler("loginall", bot.login_all_accounts))
    app.add_handler(CommandHandler("claim", bot.claim_coupon))
    app.add_handler(CommandHandler("balance", bot.check_balance))
    app.add_handler(CommandHandler("check", bot.check_status))
    app.add_handler(CommandHandler("relogin", bot.relogin_account))
    app.add_handler(CommandHandler("remove", bot.remove_account))
    app.add_handler(CommandHandler("stats", bot.stats))
    
    print("🚀 Bot is starting...")
    print(f"📊 Loaded {len(bot.accounts)} accounts")
    print("✅ Bot is ready!")
    
    app.run_polling()

if __name__ == "__main__":
    main()
