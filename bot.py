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

# ==================== IP CHECK SERVICE ====================
async def get_current_ip(client: httpx.AsyncClient) -> str:
    try:
        response = await client.get("https://api.ipify.org?format=json", timeout=5.0)
        if response.status_code == 200:
            data = response.json()
            return data.get('ip', 'Unknown')
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
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/json',
            'origin': 'https://www.cricway.io',
            'referer': 'https://www.cricway.io/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'accept-language': 'en-US,en;q=0.9',
        }
        if self.auth_token:
            self.headers['authorization'] = self.auth_token
    
    async def async_login(self, client: httpx.AsyncClient = None) -> Tuple[bool, str]:
        """Async login with full debugging"""
        json_data = {
            'username': self.username,
            'password': self.password,
            'otp': '',
            'loginRequestType': 'PHONE_SIGN_IN',
        }
        
        print(f"\n{'='*60}")
        print(f"🔍 [DEBUG] LOGIN ATTEMPT for: {self.username}")
        print(f"{'='*60}")
        print(f"📝 Request Data: {json.dumps(json_data, indent=2)}")
        
        login_headers = self.headers.copy()
        if 'authorization' in login_headers:
            del login_headers['authorization']
        
        print(f"📋 Headers being sent: {json.dumps(login_headers, indent=2)}")
        
        try:
            # Create client if not provided
            if client is None:
                print(f"🔧 Creating new HTTPX client with proxy: {PROXY_URL if PROXY_URL else 'None'}")
                async with httpx.AsyncClient(
                    http2=True, 
                    verify=False, 
                    proxy=PROXY_URL,
                    timeout=30.0,
                    follow_redirects=True
                ) as new_client:
                    return await self._do_login(new_client, json_data, login_headers)
            else:
                return await self._do_login(client, json_data, login_headers)
                
        except Exception as e:
            print(f"❌ [DEBUG] Exception: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            return False, f"Exception: {str(e)}"
    
    async def _do_login(self, client: httpx.AsyncClient, json_data: dict, headers: dict) -> Tuple[bool, str]:
        """Execute login request"""
        url = f'{self.base_url}/account/v2/login'
        print(f"🌐 Request URL: {url}")
        
        try:
            response = await client.post(url, headers=headers, json=json_data)
            
            print(f"\n📡 RESPONSE DETAILS:")
            print(f"   Status Code: {response.status_code}")
            print(f"   HTTP Version: {response.http_version}")
            print(f"   Headers: {dict(response.headers)}")
            
            # Get response text
            response_text = response.text
            print(f"   Response Body (first 500 chars): {response_text[:500]}")
            
            # Handle different status codes
            if response.status_code == 200:
                # Try to parse as JSON first
                try:
                    data = response.json()
                    print(f"   ✅ Parsed as JSON: {json.dumps(data, indent=2)[:500]}")
                    
                    # Check if response contains token
                    if isinstance(data, dict):
                        if 'data' in data and 'token' in data['data']:
                            token = data['data']['token']
                            print(f"   ✅ Token found in data.data.token")
                            return self._process_token(token)
                        elif 'token' in data:
                            token = data['token']
                            print(f"   ✅ Token found in data.token")
                            return self._process_token(token)
                        elif 'access_token' in data:
                            token = data['access_token']
                            print(f"   ✅ Token found in data.access_token")
                            return self._process_token(token)
                    
                    # If it's a plain string
                    if isinstance(data, str) and data.startswith('eyJ'):
                        print(f"   ✅ Response is raw JWT token")
                        return self._process_token(data)
                        
                except json.JSONDecodeError:
                    # Not JSON - check if it's raw JWT
                    if response_text.strip().startswith('eyJ'):
                        print(f"   ✅ Raw response is JWT token")
                        return self._process_token(response_text.strip())
                    else:
                        print(f"   ❌ Response is not JSON and not JWT")
                        return False, f"Unexpected response format: {response_text[:200]}"
            
            elif response.status_code == 400:
                print(f"   ❌ Bad Request")
                try:
                    error_data = response.json()
                    return False, f"Bad Request: {error_data.get('message', response_text[:100])}"
                except:
                    return False, f"Bad Request: {response_text[:100]}"
            
            elif response.status_code == 401:
                print(f"   ❌ Unauthorized - Invalid credentials")
                return False, "Invalid username or password"
            
            elif response.status_code == 403:
                print(f"   ❌ Forbidden - Cloudflare blocking")
                # Get proxy IP for debugging
                try:
                    ip_response = await client.get("https://api.ipify.org?format=json", timeout=5.0)
                    if ip_response.status_code == 200:
                        ip_data = ip_response.json()
                        print(f"   🌍 Current proxy IP: {ip_data.get('ip', 'Unknown')}")
                except:
                    pass
                return False, "HTTP 403 - Cloudflare is blocking this IP. Try different proxy location."
            
            else:
                print(f"   ❌ Unexpected status code")
                return False, f"HTTP {response.status_code}: {response_text[:200]}"
                
        except httpx.TimeoutException:
            print(f"   ❌ Timeout error")
            return False, "Request timeout - Server not responding"
        except httpx.ConnectError as e:
            print(f"   ❌ Connection error: {e}")
            return False, f"Connection error: {str(e)[:100]}"
        except Exception as e:
            print(f"   ❌ Unknown error: {type(e).__name__}: {e}")
            return False, f"Error: {str(e)[:100]}"
    
    def _process_token(self, token: str) -> Tuple[bool, str]:
        """Process JWT token and extract user info"""
        try:
            self.auth_token = token
            self.headers['authorization'] = self.auth_token
            
            # Extract user_id from JWT token
            import base64
            token_parts = token.split('.')
            if len(token_parts) > 1:
                payload = token_parts[1]
                # Add padding if needed
                payload += '=' * (4 - len(payload) % 4)
                decoded = base64.b64decode(payload).decode('utf-8')
                token_data = json.loads(decoded)
                self.user_id = str(token_data.get('uid', token_data.get('userId', token_data.get('sub', ''))))
                print(f"   📍 Extracted User ID: {self.user_id}")
                print(f"   📍 Token payload: {json.dumps(token_data, indent=2)[:300]}")
            
            print(f"✅ [DEBUG] Login SUCCESS for {self.username}")
            return True, "Login successful"
            
        except Exception as e:
            print(f"❌ Token processing error: {e}")
            return False, f"Token processing failed: {str(e)}"
    
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
            
            if status == 200:
                try:
                    data = response.json()
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
        
        proxy_status = "✅ Active" if PROXY_URL else "❌ Not Configured"
        
        # Test proxy connection
        proxy_ip = "Checking..."
        if PROXY_URL:
            try:
                async with httpx.AsyncClient(proxy=PROXY_URL, verify=False, timeout=10.0) as client:
                    proxy_ip = await get_current_ip(client)
            except:
                proxy_ip = "Failed to connect"
        
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
        await update.message.reply_text("🌍 Checking proxy IP...")
        
        try:
            async with httpx.AsyncClient(proxy=PROXY_URL, verify=False, timeout=10.0) as client:
                ip = await get_current_ip(client)
                await update.message.reply_text(f"🌍 Current Proxy IP: `{ip}`", parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to get proxy IP: {str(e)}")
    
    async def add_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("❌ Usage: `/add username password`", parse_mode='Markdown')
            return
        
        username, password = args[0], args[1]
        
        await update.message.reply_text(f"🔐 Verifying account *{username}* via proxy...\n⏳ Please wait...", parse_mode='Markdown')
        
        account = FastCricwayAccount(username, password)
        
        try:
            async with httpx.AsyncClient(
                http2=True, 
                verify=False, 
                proxy=PROXY_URL,
                timeout=30.0,
                follow_redirects=True
            ) as client:
                # Get proxy IP first
                proxy_ip = await get_current_ip(client)
                print(f"🌍 Using proxy IP: {proxy_ip}")
                
                success, msg = await account.async_login(client)
            
            if success:
                self.db.add_account(username, password, account.user_id, account.auth_token, proxy_ip)
                self.load_accounts()
                await update.message.reply_text(
                    f"✅ Account *{username}* added successfully!\n"
                    f"🆔 User ID: `{account.user_id}`\n"
                    f"🌍 Login IP: `{proxy_ip}`",
                    parse_mode='Markdown'
                )
            else:
                error_msg = f"❌ Failed to add *{username}*\n\nReason: `{msg}`"
                await update.message.reply_text(error_msg, parse_mode='Markdown')
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error: `{str(e)}`", parse_mode='Markdown')
    
    async def login_all_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text(f"🔄 Re-logging {len(self.accounts)} accounts via proxy...")
        
        async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL, timeout=30.0) as client:
            proxy_ip = await get_current_ip(client)
            
            tasks = [acc.async_login(client) for acc in self.accounts]
            results = await asyncio.gather(*tasks)
        
        success_count = sum(1 for r in results if r[0])
        
        result_msg = f"✅ *Login Complete!*\n"
        result_msg += f"🌍 Proxy IP: `{proxy_ip}`\n"
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
        
        await update.message.reply_text(f"⚡ Claiming *{coupon_code}* for {len(self.accounts)} accounts...")
        
        async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL, timeout=30.0) as client:
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
        
        success_count = sum(1 for r in claim_results if r[0])
        total_bonus = sum(r[2] for r in claim_results if r[0])
        
        # Save to database
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
        result_msg += f"📊 Success: {success_count}/{len(self.accounts)}\n"
        result_msg += f"💰 Total Bonus: ₹{total_bonus:.2f}"
        
        await update.message.reply_text(result_msg, parse_mode='Markdown')
    
    async def check_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.accounts:
            await update.message.reply_text("❌ No accounts found!")
            return
        
        await update.message.reply_text("💰 Fetching balances...")
        
        async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL, timeout=30.0) as client:
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
        
        await update.message.reply_text("🔍 Checking login status...")
        
        async with httpx.AsyncClient(http2=True, verify=False, proxy=PROXY_URL, timeout=30.0) as client:
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
    
    # Print configuration
    print("\n" + "="*60)
    print("🚀 CRICWAY PROXY BOT STARTING")
    print("="*60)
    print(f"🤖 Bot Token: {BOT_TOKEN[:10]}...")
    print(f"🔐 Proxy: {PROXY_TYPE.upper()}://{PROXY_HOST}:{PROXY_PORT}")
    print(f"📍 State: {PROXY_STATE or 'Auto'}")
    print(f"👤 Username: {PROXY_USER[:30]}...")
    print("="*60 + "\n")
    
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
    app.add_handler(CommandHandler("ip", bot.show_ip))
    
    print("🚀 Bot is starting...")
    print("✅ Bot is ready!\n")
    
    app.run_polling()

if __name__ == "__main__":
    main()
