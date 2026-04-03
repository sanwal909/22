import os
import json
import sqlite3
import asyncio
import aiohttp
import time
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
            'accept': 'application/json',
            'accept-language': 'en-US,en;q=0.8',
            'content-type': 'application/json',
            'origin': 'https://www.cricway.io',
            'referer': 'https://www.cricway.io/',
            'user-agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36',
            'sec-ch-ua': '"Chromium";v="146", "Not-A.Brand";v="24", "Brave";v="146"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'cross-site',
            'sec-gpc': '1',
            'priority': 'u=1, i',
        }
        if self.auth_token:
            self.headers['authorization'] = self.auth_token
    
    async def async_login(self, session: aiohttp.ClientSession) -> Tuple[bool, str]:
        """Async login using only username and password"""
        json_data = {
            'username': self.username,
            'password': self.password,
            'otp': '',
            'loginRequestType': 'PHONE_SIGN_IN',
        }
        
        print(f"🔍 [DEBUG] Attempting login for {self.username} (Credentials only)")
        
        # Ensure we don't send any old authorization header during login
        login_headers = self.headers.copy()
        if 'authorization' in login_headers:
            del login_headers['authorization']
        
        try:
            async with session.post(f'{self.base_url}/account/v2/login', 
                                   headers=login_headers, 
                                   json=json_data,
                                   timeout=aiohttp.ClientTimeout(total=10)) as response:
                status = response.status
                response_text = await response.text()
                
                if status == 200:
                    token = response_text
                    if token.startswith('eyJ'):
                        self.auth_token = token
                        self.headers['authorization'] = self.auth_token
                        
                        # Extract user_id from token
                        import base64
                        token_parts = token.split('.')
                        if len(token_parts) > 1:
                            payload = token_parts[1]
                            payload += '=' * (4 - len(payload) % 4)
                            decoded = base64.b64decode(payload).decode('utf-8')
                            token_data = json.loads(decoded)
                            self.user_id = str(token_data.get('uid', token_data.get('userId', '')))
                        
                        print(f"✅ [DEBUG] Login success for {self.username}")
                        return True, "Login successful"
                
                print(f"❌ [DEBUG] Login failed for {self.username}: HTTP {status} - {response_text[:100]}")
                return False, f"HTTP {status}"
        except asyncio.TimeoutError:
            print(f"❌ [DEBUG] Login timeout for {self.username}")
            return False, "Timeout"
        except Exception as e:
            print(f"❌ [DEBUG] Login error for {self.username}: {str(e)}")
            return False, str(e)
    
    async def async_get_balance(self, session: aiohttp.ClientSession) -> Tuple[bool, float]:
        """Async get balance"""
        if not self.auth_token:
            return False, 0
        
        try:
            async with session.get(f'{self.base_url}/wallet/v2/wallets/{self.user_id}/balance',
                                  headers=self.headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    return True, float(data.get('balance', 0))
                return False, 0
        except:
            return False, 0
    
    async def async_claim_coupon(self, session: aiohttp.ClientSession, coupon_code: str) -> Tuple[bool, str, float]:
        """Async claim coupon"""
        if not self.auth_token:
            return False, "Not authenticated", 0
        
        params = {'coupon_code': coupon_code}
        
        print(f"🔍 [DEBUG] Claiming coupon for {self.username} | Token: {self.auth_token[:20]}...")
        
        try:
            async with session.get(f'{self.base_url}/marketing/v1/bonuses/special-bonus',
                                  headers=self.headers, 
                                  params=params,
                                  timeout=aiohttp.ClientTimeout(total=10)) as response:
                status = response.status
                response_text = await response.text()
                
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
                        print(f"❌ [DEBUG] Claim failed for {self.username}: Unauthorized (Token expired?)")
                        return False, f"HTTP {status}", 0
                    else:
                        print(f"❌ [DEBUG] Claim failed for {self.username}: HTTP {status} - {api_msg}")
                        return False, f"{api_msg}", 0
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
        async with aiohttp.ClientSession() as session:
            success, msg = await account.async_login(session)
        
        if success:
            self.db.add_account(username, password, account.user_id, account.auth_token)
            self.load_accounts()  # Reload accounts
            await update.message.reply_text(
                f"✅ Account *{username}* added successfully!\n"
                f"🆔 User ID: {account.user_id}",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(f"❌ Failed to add *{username}*: {msg}", parse_mode='Markdown')
    
    async def login_all_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Auto re-login ALL accounts - Main feature"""
        await update.message.reply_text(
            f"🔄 Auto re-logging {len(self.accounts)} accounts in parallel...\n"
            f"⏱️ Estimated time: 2-3 seconds",
            parse_mode='Markdown'
        )
        
        start_time = time.time()
        
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found! Use /add to add accounts first.")
            return
        
        async with aiohttp.ClientSession() as session:
            tasks = [acc.async_login(session) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        # Update database with new tokens
        success_count = 0
        failed_accounts = []
        
        for acc, (success, msg) in zip(self.accounts, results):
            if success:
                self.db.update_account_token(acc.username, acc.auth_token, acc.user_id)
                success_count += 1
            else:
                failed_accounts.append(f"{acc.username}: {msg}")
        
        elapsed = time.time() - start_time
        
        result_msg = f"✅ *Login Complete!*\n"
        result_msg += f"⏱️ Time: {elapsed:.2f} seconds\n"
        result_msg += f"📊 Success: {success_count}/{len(self.accounts)}\n"
        
        if failed_accounts:
            result_msg += f"\n❌ *Failed Accounts:*\n"
            result_msg += "\n".join(failed_accounts[:5])
            if len(failed_accounts) > 5:
                result_msg += f"\n... and {len(failed_accounts) - 5} more"
        
        await update.message.reply_text(result_msg, parse_mode='Markdown')
    
    async def claim_coupon(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Claim coupon for ALL accounts in parallel"""
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/claim COUPON_CODE`\nExample: `/claim 200WAYCRIC`", parse_mode='Markdown')
            return
        
        coupon_code = args[0].upper()
        
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found! Use /add to add accounts first.")
            return
        
        await update.message.reply_text(
            f"⚡ Claiming *{coupon_code}* for {len(self.accounts)} accounts...\n"
            f"⏱️ Estimated time: 3-5 seconds",
            parse_mode='Markdown'
        )
        
        start_time = time.time()
        
        async with aiohttp.ClientSession() as session:
            # Helper to claim with auto-relogin
            async def claim_with_retry(acc: FastCricwayAccount):
                success, msg, bonus = await acc.async_claim_coupon(session, coupon_code)
                
                # If unauthorized, try to login and retry once
                if not success and "401" in msg:
                    print(f"🔄 [RELOGIN] Token expired for {acc.username}, attempting auto-relogin...")
                    login_success, login_msg = await acc.async_login(session)
                    if login_success:
                        # Update DB with new token
                        self.db.update_account_token(acc.username, acc.auth_token, acc.user_id)
                        # Retry claim
                        success, msg, bonus = await acc.async_claim_coupon(session, coupon_code)
                        if success:
                            msg = f"Success (after relogin)"
                        else:
                            msg = f"Failed (after relogin: {msg})"
                    else:
                        msg = f"Auth Failed: {login_msg}"
                
                return success, msg, bonus

            # Get balances BEFORE
            balance_tasks = [acc.async_get_balance(session) for acc in self.accounts]
            balances_before = await asyncio.gather(*balance_tasks)
            
            # Claim coupons with auto-retry logic
            claim_tasks = [claim_with_retry(acc) for acc in self.accounts]
            claim_results = await asyncio.gather(*claim_tasks)
            
            # Get balances AFTER
            balance_after_tasks = [acc.async_get_balance(session) for acc in self.accounts]
            balances_after = await asyncio.gather(*balance_after_tasks)
        
        elapsed = time.time() - start_time
        
        # Process results
        success_count = 0
        total_bonus = 0
        results_list = []
        
        for i, acc in enumerate(self.accounts):
            balance_before = balances_before[i][1] if balances_before[i][0] else 0
            balance_after = balances_after[i][1] if balances_after[i][0] else 0
            claim_success = claim_results[i][0]
            claim_msg = claim_results[i][1]
            bonus = claim_results[i][2]
            
            if claim_success:
                success_count += 1
                total_bonus += bonus
            
            results_list.append({
                'username': acc.username,
                'success': claim_success,
                'message': claim_msg,
                'bonus': bonus,
                'balance_before': balance_before,
                'balance_after': balance_after
            })
            
            # Save to database
            self.db.save_coupon_claim(
                acc.username, coupon_code, 
                "SUCCESS" if claim_success else "FAILED",
                bonus, balance_before, balance_after
            )
        
        # Format results
        result_msg = f"🎫 *Coupon: {coupon_code}*\n"
        result_msg += f"⚡ Time: {elapsed:.2f} seconds\n"
        result_msg += f"📊 Success: {success_count}/{len(self.accounts)}\n"
        result_msg += f"💰 Total Bonus: ₹{total_bonus:.2f}\n\n"
        
        # Show results (first 10)
        for r in results_list[:10]:
            status = "✅" if r['success'] else "❌"
            result_msg += f"{status} *{r['username']}*: "
            if r['success']:
                change = r['balance_after'] - r['balance_before']
                result_msg += f"₹{change:+.0f}"
                if r['bonus'] > 0:
                    result_msg += f" (Bonus: ₹{r['bonus']:.0f})"
            else:
                result_msg += f"{r['message']}"
            result_msg += "\n"
        
        if len(results_list) > 10:
            result_msg += f"\n... and {len(results_list) - 10} more accounts"
        
        await update.message.reply_text(result_msg, parse_mode='Markdown')
    
    async def check_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check balance of all accounts"""
        await update.message.reply_text("💰 Fetching all balances...")
        
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        start_time = time.time()
        
        async with aiohttp.ClientSession() as session:
            tasks = [acc.async_get_balance(session) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        elapsed = time.time() - start_time
        
        balance_msg = f"💰 *Balances* (fetched in {elapsed:.2f}s)\n\n"
        total_balance = 0
        active_count = 0
        
        for acc, (success, balance) in zip(self.accounts, results):
            if success:
                balance_msg += f"✅ *{acc.username}*: ₹{balance:.2f}\n"
                total_balance += balance
                active_count += 1
                self.db.save_balance(acc.username, balance)
            else:
                balance_msg += f"❌ *{acc.username}*: Failed\n"
        
        balance_msg += f"\n📊 *Summary:*\n"
        balance_msg += f"👥 Active: {active_count}/{len(self.accounts)}\n"
        balance_msg += f"💰 Total: ₹{total_balance:.2f}"
        
        await update.message.reply_text(balance_msg, parse_mode='Markdown')
    
    async def check_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check login status of all accounts"""
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text("🔍 Checking login status...")
        
        async with aiohttp.ClientSession() as session:
            tasks = [acc.async_get_balance(session) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        status_msg = "✅ *Account Status*\n\n"
        working = 0
        
        for acc, (success, _) in zip(self.accounts, results):
            if success:
                status_msg += f"✅ *{acc.username}*: Online\n"
                working += 1
            else:
                status_msg += f"❌ *{acc.username}*: Offline/Expired\n"
        
        status_msg += f"\n📊 Online: {working}/{len(self.accounts)}"
        
        await update.message.reply_text(status_msg, parse_mode='Markdown')
    
    async def relogin_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Re-login specific account using username and password"""
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/relogin username [password]`", parse_mode='Markdown')
            return
        
        username = args[0]
        password = args[1] if len(args) > 1 else None
        
        # Find account
        account = next((acc for acc in self.accounts if acc.username == username), None)
        if not account:
            await update.message.reply_text(f"❌ Account *{username}* not found!", parse_mode='Markdown')
            return
        
        # If password provided, update it in account object
        if password:
            account.password = password
            print(f"🔄 Updating password for {username}")
        
        await update.message.reply_text(f"🔄 Re-logging *{username}* using credentials...", parse_mode='Markdown')
        
        async with aiohttp.ClientSession() as session:
            success, msg = await account.async_login(session)
        
        if success:
            # Update database with new token and password (if changed)
            self.db.add_account(username, account.password, account.user_id, account.auth_token)
            await update.message.reply_text(f"✅ *{username}* re-logged successfully!", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Failed to re-login *{username}*: {msg}", parse_mode='Markdown')
    
    async def remove_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove an account"""
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/remove username`", parse_mode='Markdown')
            return
        
        username = args[0]
        self.db.delete_account(username)
        self.load_accounts()  # Reload accounts
        
        await update.message.reply_text(f"✅ Account *{username}* removed successfully!", parse_mode='Markdown')
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show statistics"""
        stats = self.db.get_stats()
        
        stats_msg = f"📊 *Bot Statistics*\n\n"
        stats_msg += f"👥 Total Accounts: {stats['total_accounts']}\n"
        stats_msg += f"📝 Today's Claims: {stats['today_claims']}\n"
        stats_msg += f"💰 Today's Bonus: ₹{stats['today_bonus']:.2f}\n"
        stats_msg += f"📊 Total Claims: {stats['total_claims']}\n"
        stats_msg += f"💎 Total Bonus: ₹{stats['total_bonus']:.2f}\n"
        
        if stats['total_claims'] > 0:
            avg_bonus = stats['total_bonus'] / stats['total_claims']
            stats_msg += f"📈 Average Bonus: ₹{avg_bonus:.2f}\n"
        
        await update.message.reply_text(stats_msg, parse_mode='Markdown')

# ==================== MAIN ====================
def main():
    # Get bot token from environment variable
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not found in environment variables!")
        print("Please set BOT_TOKEN in .env file or Railway environment variables")
        return
    
    bot = CricwayBot()
    
    # Create application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("add", bot.add_account))
    app.add_handler(CommandHandler("loginall", bot.login_all_accounts))  # Auto re-login all
    app.add_handler(CommandHandler("claim", bot.claim_coupon))
    app.add_handler(CommandHandler("balance", bot.check_balance))
    app.add_handler(CommandHandler("check", bot.check_status))
    app.add_handler(CommandHandler("relogin", bot.relogin_account))
    app.add_handler(CommandHandler("remove", bot.remove_account))
    app.add_handler(CommandHandler("stats", bot.stats))
    
    # Start bot
    print("🚀 Bot is starting on Railway...")
    print(f"📊 Loaded {len(bot.accounts)} accounts")
    print("✅ Bot is ready!")
    
    app.run_polling()

if __name__ == "__main__":
    main()