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
from concurrent.futures import ThreadPoolExecutor
import asyncio
from asyncio import Semaphore

# Load environment variables
load_dotenv()

# ==================== PROXY CONFIGURATION ====================
PROXY_HOST = os.getenv('PROXY_HOST', 'gw.dataimpulse.com')
PROXY_PORT = os.getenv('PROXY_PORT', '824')
PROXY_USER = os.getenv('PROXY_USER', '')
PROXY_PASS = os.getenv('PROXY_PASS', '')
PROXY_TYPE = os.getenv('PROXY_TYPE', 'http')
PROXY_STATE = os.getenv('PROXY_STATE', '')

def get_proxy_url():
    if not PROXY_USER or not PROXY_PASS:
        return None
    username = PROXY_USER
    if PROXY_STATE:
        username = f"{PROXY_USER};state.{PROXY_STATE}"
    encoded_user = urllib.parse.quote(username)
    encoded_pass = urllib.parse.quote(PROXY_PASS)
    return f"{PROXY_TYPE}://{encoded_user}:{encoded_pass}@{PROXY_HOST}:{PROXY_PORT}"

PROXY_URL = get_proxy_url()

# ==================== DATABASE ====================
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
                balance REAL DEFAULT 0,
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
        
        self.conn.commit()
    
    def add_account(self, username: str, password: str, user_id: str = None, auth_token: str = None, last_ip: str = None, balance: float = 0) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO accounts (username, password, user_id, auth_token, last_ip, balance, is_active, last_login) VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
                (username, password, user_id, auth_token, last_ip, balance)
            )
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error adding account: {e}")
            return False
    
    def get_all_accounts(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT username, password, user_id, auth_token, last_ip, balance, is_active FROM accounts WHERE is_active = 1")
        rows = cursor.fetchall()
        return [
            {
                "username": row[0],
                "password": row[1],
                "user_id": row[2],
                "auth_token": row[3],
                "last_ip": row[4],
                "balance": row[5] or 0,
                "is_active": bool(row[6])
            }
            for row in rows
        ]
    
    def update_account(self, username: str, auth_token: str = None, user_id: str = None, last_ip: str = None, balance: float = None):
        cursor = self.conn.cursor()
        updates = []
        params = []
        
        if auth_token:
            updates.append("auth_token = ?")
            params.append(auth_token)
        if user_id:
            updates.append("user_id = ?")
            params.append(user_id)
        if last_ip:
            updates.append("last_ip = ?")
            params.append(last_ip)
        if balance is not None:
            updates.append("balance = ?")
            params.append(balance)
        
        if updates:
            updates.append("last_login = CURRENT_TIMESTAMP")
            params.append(username)
            cursor.execute(f"UPDATE accounts SET {', '.join(updates)} WHERE username = ?", params)
            self.conn.commit()
    
    def save_coupon_claim(self, username: str, coupon_code: str, status: str, bonus: float, balance_before: float, balance_after: float, proxy_ip: str = None):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO coupon_history (username, coupon_code, status, bonus, balance_before, balance_after, proxy_ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, coupon_code, status, bonus, balance_before, balance_after, proxy_ip)
        )
        self.conn.commit()
        
        # Update account balance
        if status == "SUCCESS":
            cursor.execute("UPDATE accounts SET balance = ? WHERE username = ?", (balance_after, username))
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
        cursor.execute("SELECT SUM(balance) FROM accounts WHERE is_active = 1")
        total_balance = cursor.fetchone()[0] or 0
        return {
            'total_accounts': total_accounts,
            'today_claims': today_claims,
            'today_bonus': today_bonus,
            'total_claims': total_claims or 0,
            'total_bonus': total_bonus or 0,
            'total_balance': total_balance
        }

# ==================== FAST API CLIENT ====================
class FastCricwayAccount:
    def __init__(self, username: str, password: str, auth_token: str = None, user_id: str = None):
        self.username = username
        self.password = password
        self.auth_token = auth_token
        self.user_id = user_id
        self.balance = 0
        self.base_url = "https://api.uvwin2024.co"
        self.headers = {
            'accept': 'application/json',
            'content-type': 'application/json',
            'origin': 'https://www.cricway.io',
            'referer': 'https://www.cricway.io/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }
        if self.auth_token:
            self.headers['authorization'] = self.auth_token
    
    async def fast_login(self, client: httpx.AsyncClient) -> Tuple[bool, str]:
        """Fast login - returns token and user_id"""
        json_data = {
            'username': self.username,
            'password': self.password,
            'otp': '',
            'loginRequestType': 'PHONE_SIGN_IN',
        }
        
        login_headers = self.headers.copy()
        if 'authorization' in login_headers:
            del login_headers['authorization']
        
        try:
            response = await client.post(
                f'{self.base_url}/account/v2/login',
                headers=login_headers,
                json=json_data,
                timeout=10.0
            )
            
            if response.status_code == 200:
                token = response.text.strip()
                if token.startswith('eyJ'):
                    self.auth_token = token
                    self.headers['authorization'] = self.auth_token
                    
                    # Extract user_id from JWT
                    import base64
                    token_parts = token.split('.')
                    if len(token_parts) > 1:
                        payload = token_parts[1]
                        payload += '=' * (4 - len(payload) % 4)
                        decoded = base64.b64decode(payload).decode('utf-8')
                        token_data = json.loads(decoded)
                        self.user_id = str(token_data.get('uid', token_data.get('userId', '')))
                    
                    return True, "Login successful"
            return False, f"HTTP {response.status_code}"
        except Exception as e:
            return False, str(e)
    
    async def fast_balance(self, client: httpx.AsyncClient) -> Tuple[bool, float]:
        """Fast balance check"""
        if not self.auth_token:
            return False, 0
        
        try:
            response = await client.get(
                f'{self.base_url}/wallet/v2/wallets/{self.user_id}/balance',
                headers=self.headers,
                timeout=8.0
            )
            if response.status_code == 200:
                data = response.json()
                self.balance = float(data.get('balance', 0))
                return True, self.balance
            return False, 0
        except:
            return False, 0
    
    async def fast_claim(self, client: httpx.AsyncClient, coupon_code: str) -> Tuple[bool, str, float]:
        """Fast coupon claim"""
        if not self.auth_token:
            return False, "Not authenticated", 0
        
        params = {'coupon_code': coupon_code}
        
        try:
            response = await client.get(
                f'{self.base_url}/marketing/v1/bonuses/special-bonus',
                headers=self.headers,
                params=params,
                timeout=8.0
            )
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    bonus = data.get('data', {}).get('amount', 0)
                    return True, "Success", float(bonus)
                except:
                    return True, "Claimed", 0
            elif response.status_code == 409:
                return False, "Limit exhausted", 0
            else:
                return False, f"HTTP {response.status_code}", 0
        except Exception as e:
            return False, str(e), 0

# ==================== ULTRA FAST BOT ====================
class UltraFastBot:
    def __init__(self):
        self.db = Database()
        self.accounts = []
        self.semaphore = Semaphore(50)  # Max 50 concurrent requests
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
        
        msg = f"""
🚀 *ULTRA-FAST CRICWAY BOT* 🚀

📊 *STATISTICS*
├ 👥 Accounts: `{stats['total_accounts']}`
├ 💰 Total Balance: `₹{stats['total_balance']:.2f}`
├ 📊 Today Claims: `{stats['today_claims']}`
├ 💎 Today Bonus: `₹{stats['today_bonus']:.2f}`
└ 🏆 Total Bonus: `₹{stats['total_bonus']:.2f}`

⚡ *COMMANDS*
├ `/add user pass` - Add account
├ `/loginall` - Login all accounts
├ `/claim CODE` - Claim coupon (ULTRA FAST!)
├ `/balance` - Show all balances
├ `/check` - Check login status
├ `/stats` - View statistics
└ `/remove user` - Remove account

🎯 *SPEED: 50 accounts in 3-5 seconds!*
        """
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def add_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: `/add username password`", parse_mode='Markdown')
            return
        
        username, password = args[0], args[1]
        
        status_msg = await update.message.reply_text(f"🔐 Adding *{username}*...", parse_mode='Markdown')
        
        account = FastCricwayAccount(username, password)
        
        try:
            async with httpx.AsyncClient(proxy=PROXY_URL, verify=False, timeout=15.0) as client:
                success, msg = await account.fast_login(client)
                
                if success:
                    # Get initial balance
                    bal_success, balance = await account.fast_balance(client)
                    
                    self.db.add_account(username, password, account.user_id, account.auth_token, None, balance)
                    self.load_accounts()
                    
                    await status_msg.edit_text(
                        f"✅ *{username}* ADDED!\n"
                        f"├ 🆔 ID: `{account.user_id}`\n"
                        f"└ 💰 Balance: `₹{balance:.2f}`",
                        parse_mode='Markdown'
                    )
                else:
                    await status_msg.edit_text(f"❌ Failed: `{msg}`", parse_mode='Markdown')
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: `{str(e)[:100]}`", parse_mode='Markdown')
    
    async def login_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        status_msg = await update.message.reply_text(f"🔄 Logging in {len(self.accounts)} accounts...\n⏱️ Estimated: 2-3 seconds")
        start_time = time.time()
        
        async with httpx.AsyncClient(proxy=PROXY_URL, verify=False, timeout=15.0) as client:
            tasks = [acc.fast_login(client) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        success = sum(1 for r in results if r[0])
        elapsed = time.time() - start_time
        
        await status_msg.edit_text(
            f"✅ *LOGIN COMPLETE*\n"
            f"├ ⚡ Time: `{elapsed:.2f}s`\n"
            f"├ 📊 Success: `{success}/{len(self.accounts)}`\n"
            f"└ 💰 Total Balance: Updating...",
            parse_mode='Markdown'
        )
        
        # Update balances
        async with httpx.AsyncClient(proxy=PROXY_URL, verify=False, timeout=15.0) as client:
            balance_tasks = [acc.fast_balance(client) for acc in self.accounts]
            balance_results = await asyncio.gather(*balance_tasks)
        
        total_balance = sum(b[1] for b in balance_results if b[0])
        
        for acc, (success, balance) in zip(self.accounts, balance_results):
            if success:
                self.db.update_account(acc.username, acc.auth_token, acc.user_id, None, balance)
        
        await status_msg.edit_text(
            f"✅ *LOGIN COMPLETE*\n"
            f"├ ⚡ Time: `{elapsed:.2f}s`\n"
            f"├ 📊 Success: `{success}/{len(self.accounts)}`\n"
            f"└ 💰 Total Balance: `₹{total_balance:.2f}`",
            parse_mode='Markdown'
        )
    
    async def claim_coupon(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/claim COUPON_CODE`\nExample: `/claim 200WAYCRIC`", parse_mode='Markdown')
            return
        
        coupon_code = args[0].upper()
        
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        status_msg = await update.message.reply_text(
            f"🎫 *CLAIMING* `{coupon_code}`\n"
            f"├ 👥 Accounts: `{len(self.accounts)}`\n"
            f"├ ⚡ Speed: ULTRA FAST\n"
            f"└ ⏱️ Estimated: 3-5 seconds",
            parse_mode='Markdown'
        )
        
        start_time = time.time()
        
        async with httpx.AsyncClient(proxy=PROXY_URL, verify=False, timeout=15.0) as client:
            # Get balances BEFORE (parallel)
            before_tasks = [acc.fast_balance(client) for acc in self.accounts]
            before_results = await asyncio.gather(*before_tasks)
            
            # Claim coupons (parallel)
            claim_tasks = [acc.fast_claim(client, coupon_code) for acc in self.accounts]
            claim_results = await asyncio.gather(*claim_tasks)
            
            # Get balances AFTER (parallel)
            after_tasks = [acc.fast_balance(client) for acc in self.accounts]
            after_results = await asyncio.gather(*after_tasks)
        
        elapsed = time.time() - start_time
        
        # Process results
        results = []
        success_count = 0
        total_bonus = 0
        total_balance_before = 0
        total_balance_after = 0
        
        for i, acc in enumerate(self.accounts):
            before_balance = before_results[i][1] if before_results[i][0] else 0
            after_balance = after_results[i][1] if after_results[i][0] else 0
            claim_success = claim_results[i][0]
            claim_msg = claim_results[i][1]
            bonus = claim_results[i][2]
            
            if claim_success:
                success_count += 1
                total_bonus += bonus
                total_balance_before += before_balance
                total_balance_after += after_balance
            else:
                total_balance_before += before_balance
                total_balance_after += before_balance
            
            results.append({
                'username': acc.username,
                'success': claim_success,
                'message': claim_msg,
                'bonus': bonus,
                'before': before_balance,
                'after': after_balance
            })
            
            # Save to database
            self.db.save_coupon_claim(
                acc.username, coupon_code,
                "SUCCESS" if claim_success else "FAILED",
                bonus, before_balance, after_balance
            )
        
        # Calculate total change
        total_change = total_balance_after - total_balance_before
        
        # Format response
        response = f"🎫 *COUPON: {coupon_code}*\n"
        response += f"├ ⚡ Time: `{elapsed:.2f} seconds`\n"
        response += f"├ 📊 Success: `{success_count}/{len(self.accounts)}`\n"
        response += f"├ 💰 Bonus: `₹{total_bonus:.2f}`\n"
        response += f"└ 📈 Total Change: `₹{total_change:+.2f}`\n\n"
        
        # Show top 10 results
        response += "*DETAILS:*\n"
        
        # Sort by bonus (highest first)
        results.sort(key=lambda x: x['bonus'], reverse=True)
        
        for r in results[:15]:
            if r['success']:
                change = r['after'] - r['before']
                if r['bonus'] > 0:
                    response += f"✅ *{r['username'][:15]}*: +₹{change:.0f} 💰\n"
                else:
                    response += f"✅ *{r['username'][:15]}*: ₹{r['after']:.0f}\n"
            else:
                response += f"❌ *{r['username'][:15]}*: {r['message'][:20]}\n"
        
        if len(results) > 15:
            response += f"\n*... and {len(results)-15} more accounts*"
        
        # Add summary
        response += f"\n📊 *SUMMARY*\n"
        response += f"├ 💰 Before: `₹{total_balance_before:.2f}`\n"
        response += f"├ 💰 After: `₹{total_balance_after:.2f}`\n"
        response += f"└ 📈 Change: `₹{total_change:+.2f}`"
        
        await status_msg.edit_text(response, parse_mode='Markdown')
    
    async def show_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        status_msg = await update.message.reply_text("💰 Fetching balances...")
        
        async with httpx.AsyncClient(proxy=PROXY_URL, verify=False, timeout=15.0) as client:
            tasks = [acc.fast_balance(client) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        # Update database
        for acc, (success, balance) in zip(self.accounts, results):
            if success:
                self.db.update_account(acc.username, None, None, None, balance)
        
        response = "💰 *ALL BALANCES*\n\n"
        total = 0
        online = 0
        
        for acc, (success, balance) in zip(self.accounts, results):
            if success:
                response += f"✅ *{acc.username}*: `₹{balance:.2f}`\n"
                total += balance
                online += 1
            else:
                response += f"❌ *{acc.username}*: `Offline`\n"
        
        response += f"\n📊 *SUMMARY*\n"
        response += f"├ 👥 Online: `{online}/{len(self.accounts)}`\n"
        response += f"└ 💰 Total: `₹{total:.2f}`"
        
        await status_msg.edit_text(response, parse_mode='Markdown')
    
    async def check_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text("🔍 Checking status...")
        
        async with httpx.AsyncClient(proxy=PROXY_URL, verify=False, timeout=15.0) as client:
            tasks = [acc.fast_balance(client) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        online = sum(1 for r in results if r[0])
        
        response = f"✅ *STATUS*\n"
        response += f"├ 👥 Online: `{online}/{len(self.accounts)}`\n"
        
        if online == len(self.accounts):
            response += f"└ 🟢 All accounts online!"
        elif online > 0:
            response += f"└ 🟡 {len(self.accounts)-online} accounts offline"
        else:
            response += f"└ 🔴 All accounts offline!"
        
        await update.message.reply_text(response, parse_mode='Markdown')
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = self.db.get_stats()
        
        # Calculate average bonus
        avg_bonus = stats['total_bonus'] / stats['total_claims'] if stats['total_claims'] > 0 else 0
        
        response = f"📊 *BOT STATISTICS*\n\n"
        response += f"👥 *ACCOUNTS*\n"
        response += f"├ Total: `{stats['total_accounts']}`\n"
        response += f"└ Active: `{stats['total_accounts']}`\n\n"
        
        response += f"💰 *BALANCE*\n"
        response += f"└ Total: `₹{stats['total_balance']:.2f}`\n\n"
        
        response += f"🎫 *COUPONS*\n"
        response += f"├ Total Claims: `{stats['total_claims']}`\n"
        response += f"├ Today Claims: `{stats['today_claims']}`\n"
        response += f"├ Total Bonus: `₹{stats['total_bonus']:.2f}`\n"
        response += f"├ Today Bonus: `₹{stats['today_bonus']:.2f}`\n"
        response += f"└ Average Bonus: `₹{avg_bonus:.2f}`"
        
        await update.message.reply_text(response, parse_mode='Markdown')
    
    async def remove_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("❌ Usage: `/remove username`", parse_mode='Markdown')
            return
        
        username = args[0]
        self.db.delete_account(username)
        self.load_accounts()
        
        await update.message.reply_text(f"✅ *{username}* removed!", parse_mode='Markdown')

# ==================== MAIN ====================
def main():
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not found!")
        return
    
    # Print configuration
    print("\n" + "="*50)
    print("🚀 ULTRA-FAST CRICWAY BOT")
    print("="*50)
    if PROXY_URL:
        print(f"✅ Proxy: {PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}")
    else:
        print("⚠️ No proxy configured")
    print("="*50 + "\n")
    
    bot = UltraFastBot()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("add", bot.add_account))
    app.add_handler(CommandHandler("loginall", bot.login_all))
    app.add_handler(CommandHandler("claim", bot.claim_coupon))
    app.add_handler(CommandHandler("balance", bot.show_balance))
    app.add_handler(CommandHandler("check", bot.check_status))
    app.add_handler(CommandHandler("stats", bot.show_stats))
    app.add_handler(CommandHandler("remove", bot.remove_account))
    
    print("🚀 Bot is running...")
    print("✅ Ready for ultra-fast coupon claiming!\n")
    
    app.run_polling()

if __name__ == "__main__":
    main()
