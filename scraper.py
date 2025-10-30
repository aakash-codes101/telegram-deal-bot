import asyncio
import os
import json
import re
import html
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from telethon.sync import TelegramClient
from telegram import Bot
from telegram.constants import ParseMode

# --- Configuration ---
API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TRACKING_ID = os.getenv('AMAZON_TRACKING_ID')

# --- File & Directory Names ---
SESSION_NAME = 'my_telegram_user_session'
POSTED_LINKS_FILE = 'posted_links.json'
IMAGE_SAVE_DIR = 'product_images'

# --- Helper Functions ---

def load_posted_links():
    if os.path.exists(POSTED_LINKS_FILE):
        with open(POSTED_LINKS_FILE, 'r') as f:
            try: return set(json.load(f))
            except json.JSONDecodeError: return set()
    return set()

def save_posted_links(links_set):
    with open(POSTED_LINKS_FILE, 'w') as f:
        json.dump(list(links_set), f)

def find_links_in_text(text):
    return re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text)

def get_clean_amazon_url(url):
    match = re.search(r"(https://www\.amazon\.in/.*?/dp/[A-Z0-9]{10})", url)
    if match: return match.group(1)
    return None

def get_product_asin(product_url):
    """Extracts the unique ASIN from an Amazon URL."""
    match = re.search(r"/dp/([A-Z0-9]{10})", product_url)
    return match.group(1) if match else None

def convert_to_affiliate_link(url, tracking_id):
    if not tracking_id or not url: return url
    return f"{url}/?tag={tracking_id}"

async def resolve_short_link(session, url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    try:
        async with session.get(url, headers=headers, timeout=10, allow_redirects=True) as response:
            return str(response.url)
    except Exception:
        return None

# --- Core Logic ---

async def scan_and_process_links(client, posted_links, session):
    """Scans messages, pre-filters for Amazon-related links, resolves them, and returns new product links."""
    new_clean_amazon_links = set()
    
    # NOTE: This is set to 30 minutes. Change 'minutes=30' to 'hours=24' if you want to scan all day.
    time_window_ago = datetime.utcnow() - timedelta(minutes=30)
    
    print("Scanning dialogs for new messages...")
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            try:
                async for message in client.iter_messages(dialog.entity, limit=200):
                    if message.date.replace(tzinfo=None) < time_window_ago: break
                    if message.text:
                        found_links = find_links_in_text(message.text)
                        for link in found_links:
                            if 'amazon' in link or 'amzn' in link:
                                resolved_url = await resolve_short_link(session, link)
                                if resolved_url:
                                    clean_link = get_clean_amazon_url(resolved_url)
                                    if clean_link and clean_link not in posted_links:
                                        new_clean_amazon_links.add(clean_link)
            except ConnectionError:
                print(f"⚠️ Connection lost while scanning '{dialog.title}'. Reconnecting and continuing...")
                await client.connect()
                continue
            except Exception as e:
                print(f"An error occurred while scanning '{dialog.title}': {e}")
                continue
                
    print(f"Found {len(new_clean_amazon_links)} new Amazon links to process.")
    return list(new_clean_amazon_links)

async def fetch_product_details(session, product_url):
    """Scrapes Amazon page for price and downloads images using aiohttp."""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36', 'Accept-Language': 'en-US,en;q=0.9'}
    print(f"🔗 Scraping {product_url}...")
    try:
        async with session.get(product_url, headers=headers, timeout=20) as response:
            response.raise_for_status()
            html_content = await response.text()
            soup = BeautifulSoup(html_content, 'html.parser')

        price_span = soup.select_one('.a-price-whole')
        price = "Price not found"
        if price_span: price = f"₹{price_span.get_text(strip=True).replace(',', '')}"
        title_span = soup.select_one('#productTitle')
        if not title_span:
            print("❌ Could not find product title. Skipping.")
            return None
        title = title_span.get_text(strip=True)

        image_tag = soup.find('img', {'id': 'landingImage'})
        remote_image_urls = []
        if image_tag and 'data-a-dynamic-image' in image_tag.attrs:
            image_data = json.loads(image_tag['data-a-dynamic-image'])
            remote_image_urls = list(image_data.keys())
        if not remote_image_urls: print("⚠️ Could not find images. Will post a text-only link.")
        
        local_image_paths = []
        if remote_image_urls:
            asin = get_product_asin(product_url)
            if not asin: 
                print("❌ Could not parse ASIN from URL. Skipping image download.")
            else:
                product_dir = os.path.join(IMAGE_SAVE_DIR, asin)
                os.makedirs(product_dir, exist_ok=True)
                for i, img_url in enumerate(remote_image_urls[:5]):
                    async with session.get(img_url, timeout=10) as img_response:
                        img_response.raise_for_status()
                        local_path = os.path.join(product_dir, f'image_{i + 1}.jpg')
                        with open(local_path, 'wb') as f:
                            f.write(await img_response.read())
                        local_image_paths.append(local_path)
        
        print(f"✅ Scraped '{title}' successfully.")
        return {"title": title, "price": price, "image_paths": local_image_paths, "url": product_url}

    except Exception as e:
        print(f"❌ Scraping failed for {product_url}: {e}")
        return None

async def post_update_to_telegram(bot, product_data):
    """Formats and sends the product update to the specified Telegram group using HTML."""
    title = html.escape(product_data['title'])
    price = html.escape(product_data['price'])
    affiliate_url = convert_to_affiliate_link(product_data['url'], TRACKING_ID)
    caption = (f"🔥 <b>{title}</b>\n\n💰 <b>Price:</b> <code>{price}</code>\n\n🔗 <a href=\"{affiliate_url}\">Buy Now on Amazon</a>")
    image_paths = product_data['image_paths']
    print(f"📢 Posting '{product_data['title']}' to Telegram...")
    try:
        if image_paths:
             await bot.send_photo(chat_id=CHAT_ID, photo=open(image_paths[0], 'rb'), caption=caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=CHAT_ID, text=caption, parse_mode=ParseMode.HTML)
        print("✅ Posted successfully!")
        return True
    except Exception as e:
        print(f"❌ Failed to post to Telegram: {e}")
        return False

async def main():
    """Main function to run the bot logic."""
    if not all([API_ID, API_HASH, BOT_TOKEN, CHAT_ID, TRACKING_ID]):
        print("🔴 Missing one or more required environment variables/secrets. Exiting.")
        return

    posted_links = load_posted_links()
    
    async with aiohttp.ClientSession() as http_session:
        async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
            new_links = await scan_and_process_links(client, posted_links, http_session)
        
        bot = Bot(token=BOT_TOKEN)
        
        if not new_links:
            print("No new Amazon links to process. Posting status message.")
            try:
                await bot.send_message(chat_id=CHAT_ID, text="Nothing posted this time.")
                print("✅ Status message posted successfully.")
            except Exception as e:
                print(f"❌ Failed to post status message: {e}")
            return
            
        for link in new_links:
            details = await fetch_product_details(http_session, link)
            if details:
                success = await post_update_to_telegram(bot, details)
                if success:
                    posted_links.add(link)
    
    save_posted_links(posted_links)
    print("\n✨ Process complete.")

if __name__ == "__main__":
    asyncio.run(main())
