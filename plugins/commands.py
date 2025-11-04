import os
import re, sys
import json
import base64
import logging
import random
import asyncio
import string
import pytz
from .pmfilter import auto_filter 
from Script import script
from datetime import datetime
from database.refer import referdb
from database.config_db import mdb
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChatAdminRequired, UserNotParticipant
from database.ia_filterdb import Media, Media2, get_file_details, unpack_new_file_id, get_bad_files
from database.users_chats_db import db
from info import *
from utils import get_settings, save_group_settings, is_subscribed, is_req_subscribed, get_size, get_shortlink, is_check_admin, temp, get_readable_time, get_time, generate_settings_text, log_error, clean_filename
import time

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)
BOT_USERNAME = "VVFilter_bot"
TIMEZONE = "Asia/Kolkata"
BATCH_FILES = {}

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from bson import ObjectId
import os
from typing import Dict
import asyncio
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration - Replace with your values

# MongoDB setup
mongo_client = AsyncIOMotorClient(DATABASE_URI)
bd = mongo_client["filestore"]
files_collection = bd["files"]
batches_collection = bd["batches"]
downloads_collection = bd["downloads"]

# Temporary storage for batch operations
batch_sessions: Dict[int, Dict] = {}

# Initialize bot
app = Client


def format_size(size_bytes: int) -> str:
    """Format file size to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


async def get_file_info(message: Message) -> dict:
    """Extract file information from message"""
    file_info = {
        "file_id": None,
        "file_unique_id": None,
        "file_name": None,
        "file_size": 0,
        "mime_type": None,
        "file_type": None
    }
    
    if message.document:
        file_info.update({
            "file_id": message.document.file_id,
            "file_unique_id": message.document.file_unique_id,
            "file_name": message.document.file_name,
            "file_size": message.document.file_size,
            "mime_type": message.document.mime_type,
            "file_type": "document"
        })
    elif message.video:
        file_info.update({
            "file_id": message.video.file_id,
            "file_unique_id": message.video.file_unique_id,
            "file_name": message.video.file_name or "video.mp4",
            "file_size": message.video.file_size,
            "mime_type": message.video.mime_type,
            "file_type": "video"
        })
    elif message.audio:
        file_info.update({
            "file_id": message.audio.file_id,
            "file_unique_id": message.audio.file_unique_id,
            "file_name": message.audio.file_name or "audio.mp3",
            "file_size": message.audio.file_size,
            "mime_type": message.audio.mime_type,
            "file_type": "audio"
        })
    elif message.photo:
        photo = message.photo
        file_info.update({
            "file_id": photo.file_id,
            "file_unique_id": photo.file_unique_id,
            "file_name": f"photo_{photo.file_unique_id}.jpg",
            "file_size": photo.file_size,
            "mime_type": "image/jpeg",
            "file_type": "photo"
        })
    
    return file_info


# ==================== STEP 1: RECEIVE FILE & ASK SINGLE/MULTI ====================

@app.on_message(filters.private & filters.media & filters.user(ADMINS))
async def handle_incoming_media(client: Client, message: Message):
    """Handle incoming media from admins"""
    
    try:
        user_id = message.from_user.id
        
        # Check if user is in batch mode (already collecting files)
        if user_id in batch_sessions and batch_sessions[user_id].get("active"):
            # Add file to batch
            file_info = await get_file_info(message)
            file_info["message"] = message  # Store message object temporarily
            batch_sessions[user_id]["files"].append(file_info)
            
            file_count = len(batch_sessions[user_id]["files"])
            await message.reply_text(
                f"✅ **File {file_count} added to batch**\n\n"
                f"📁 {file_info['file_name']}\n"
                f"💾 Size: {format_size(file_info['file_size'])}\n\n"
                f"Send more files or click **Done** below.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Done - Generate Link", callback_data=f"batch_done_{user_id}")]
                ])
            )
            return
        
        # First file - Ask single or multi
        file_info = await get_file_info(message)
        file_info["message"] = message  # Store message object
        
        # Initialize batch session
        batch_sessions[user_id] = {
            "active": False,
            "files": [file_info],
            "timestamp": datetime.utcnow()
        }
        
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📄 Single File", callback_data=f"single_{user_id}"),
                InlineKeyboardButton("📦 Multi Files", callback_data=f"multi_start_{user_id}")
            ]
        ])
        
        await message.reply_text(
            f"📥 **Media Received!**\n\n"
            f"📁 {file_info['file_name']}\n"
            f"💾 Size: {format_size(file_info['file_size'])}\n"
            f"📝 Type: {file_info['file_type'].upper()}\n\n"
            f"**Choose an option:**\n"
            f"• Single File - Generate link for this file only\n"
            f"• Multi Files - Add more files and create one link",
            reply_markup=buttons
        )
            
    except Exception as e:
        logger.error(f"Error in handle_incoming_media: {e}", exc_info=True)
        await message.reply_text("❌ An error occurred. Please try again.")


# ==================== STEP 2: SINGLE FILE - FORWARD TO CHANNEL, STORE, GENERATE LINK ====================

@app.on_callback_query(filters.regex(r"^single_"))
async def handle_single_file(client: Client, callback: CallbackQuery):
    """Handle single file: forward to channel, store in DB, generate link"""
    
    try:
        user_id = int(callback.data.split("_")[1])
        
        if user_id not in batch_sessions:
            await callback.answer("Session expired. Please send file again.", show_alert=True)
            return
        
        await callback.message.edit_text("⏳ **Processing...**\n\nForwarding file to storage...")
        
        # Get the file
        file_info = batch_sessions[user_id]["files"][0]
        original_message = file_info["message"]
        
        # STEP 1: Forward to channel and get channel message ID
        if not CHANNELS:
            await callback.message.edit_text("❌ Storage channel not configured!")
            return
        
        channel_id = CHANNELS[0]
        try:
            # Forward message to channel
            forwarded_msg = await original_message.copy(channel_id)
            channel_msg_id = forwarded_msg.id
            logger.info(f"File forwarded to channel {channel_id}, message ID: {channel_msg_id}")
        except Exception as e:
            logger.error(f"Error forwarding to channel: {e}", exc_info=True)
            await callback.message.edit_text(f"❌ Error forwarding to storage channel: {e}")
            return
        
        # STEP 2: Store in database with channel message ID
        file_doc = {
            "user_id": user_id,
            "file_id": file_info["file_id"],
            "file_unique_id": file_info["file_unique_id"],
            "file_name": file_info["file_name"],
            "file_size": file_info["file_size"],
            "mime_type": file_info["mime_type"],
            "file_type": file_info["file_type"],
            "caption": original_message.caption,
            "channel_id": channel_id,
            "channel_msg_id": channel_msg_id,  # Store channel message ID
            "is_batch": False,
            "created_at": datetime.utcnow()
        }
        
        result = await files_collection.insert_one(file_doc)
        store_id = str(result.inserted_id)  # This is our unique store ID
        
        logger.info(f"File stored in DB with ID: {store_id}")
        
        # STEP 3: Generate link with store ID
        link = f"https://t.me/{BOT_USERNAME}?start=file_{store_id}"
        
        # Clear session
        del batch_sessions[user_id]
        
        # Send success message with link
        await callback.message.edit_text(
            f"✅ **Single File Link Generated!**\n\n"
            f"📁 {file_info['file_name']}\n"
            f"💾 {format_size(file_info['file_size'])}\n"
            f"📝 Type: {file_info['file_type'].upper()}\n\n"
            f"🔗 **Share Link:**\n`{link}`\n\n"
            f"📋 Tap to copy the link",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Open Link", url=link)]
            ])
        )
        
    except Exception as e:
        logger.error(f"Error in handle_single_file: {e}", exc_info=True)
        await callback.message.edit_text(f"❌ Error: {e}")


# ==================== STEP 3: MULTI FILES - START BATCH MODE ====================

@app.on_callback_query(filters.regex(r"^multi_start_"))
async def handle_multi_start(client: Client, callback: CallbackQuery):
    """Start multi/batch mode"""
    
    try:
        user_id = int(callback.data.split("_")[2])
        
        if user_id not in batch_sessions:
            await callback.answer("Session expired. Please send files again.", show_alert=True)
            return
        
        # Activate batch mode
        batch_sessions[user_id]["active"] = True
        
        file_count = len(batch_sessions[user_id]["files"])
        
        await callback.message.edit_text(
            f"📦 **Multi-File Mode Activated!**\n\n"
            f"✅ {file_count} file(s) already added\n\n"
            f"📤 Send all files you want to include.\n"
            f"When finished, click **Done** to generate the link.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Done - Generate Link", callback_data=f"batch_done_{user_id}")]
            ])
        )
        
        await callback.answer("Multi-file mode activated! Send files now.")
        
    except Exception as e:
        logger.error(f"Error in handle_multi_start: {e}", exc_info=True)
        await callback.answer("Error activating batch mode", show_alert=True)


# ==================== STEP 4: BATCH DONE - FORWARD ALL, STORE, GENERATE LINK ====================

@app.on_callback_query(filters.regex(r"^batch_done_"))
async def handle_batch_done(client: Client, callback: CallbackQuery):
    """Complete batch: forward all files to channel, store in DB, generate link"""
    
    try:
        user_id = int(callback.data.split("_")[2])
        
        if user_id not in batch_sessions:
            await callback.answer("Session expired.", show_alert=True)
            return
        
        files = batch_sessions[user_id]["files"]
        
        if len(files) == 0:
            await callback.answer("No files to process!", show_alert=True)
            return
        
        await callback.message.edit_text(
            f"⏳ **Processing {len(files)} files...**\n\n"
            f"Forwarding to storage channel..."
        )
        
        if not CHANNELS:
            await callback.message.edit_text("❌ Storage channel not configured!")
            return
        
        channel_id = CHANNELS[0]
        stored_files = []
        
        # STEP 1: Forward all files to channel and store each with channel msg ID
        for idx, file_info in enumerate(files, 1):
            try:
                original_message = file_info["message"]
                
                # Forward to channel
                forwarded_msg = await original_message.copy(channel_id)
                channel_msg_id = forwarded_msg.id
                
                # Store individual file in DB
                file_doc = {
                    "user_id": user_id,
                    "file_id": file_info["file_id"],
                    "file_unique_id": file_info["file_unique_id"],
                    "file_name": file_info["file_name"],
                    "file_size": file_info["file_size"],
                    "mime_type": file_info["mime_type"],
                    "file_type": file_info["file_type"],
                    "caption": original_message.caption,
                    "channel_id": channel_id,
                    "channel_msg_id": channel_msg_id,  # Individual channel message ID
                    "is_batch": True,
                    "created_at": datetime.utcnow()
                }
                
                result = await files_collection.insert_one(file_doc)
                file_store_id = str(result.inserted_id)
                
                stored_files.append({
                    "store_id": file_store_id,
                    "name": file_info["file_name"],
                    "size": file_info["file_size"],
                    "type": file_info["file_type"],
                    "channel_msg_id": channel_msg_id
                })
                
                logger.info(f"File {idx}/{len(files)} stored: {file_store_id}")
                
            except Exception as e:
                logger.error(f"Error processing file {idx}: {e}", exc_info=True)
                continue
        
        if len(stored_files) == 0:
            await callback.message.edit_text("❌ Failed to store any files!")
            return
        
        # STEP 2: Create batch document with all store IDs
        batch_doc = {
            "user_id": user_id,
            "file_store_ids": [f["store_id"] for f in stored_files],  # Store IDs of individual files
            "file_count": len(stored_files),
            "total_size": sum(f["size"] for f in stored_files),
            "channel_id": channel_id,
            "files_info": stored_files,
            "created_at": datetime.utcnow()
        }
        
        result = await batches_collection.insert_one(batch_doc)
        batch_store_id = str(result.inserted_id)  # This is our batch store ID
        
        logger.info(f"Batch created with ID: {batch_store_id}")
        
        # STEP 3: Generate link with batch store ID
        link = f"https://t.me/{BOT_USERNAME}?start=batch_{batch_store_id}"
        
        # Create file list
        files_list = "\n".join([
            f"{i+1}. 📁 {f['name']} ({format_size(f['size'])})"
            for i, f in enumerate(stored_files)
        ])
        
        total_size = format_size(batch_doc["total_size"])
        
        await callback.message.edit_text(
            f"✅ **Batch Link Generated!**\n\n"
            f"📦 **Batch Details:**\n"
            f"📊 Files: {len(stored_files)}\n"
            f"💾 Total Size: {total_size}\n\n"
            f"**Files in batch:**\n{files_list}\n\n"
            f"🔗 **Share Link:**\n`{link}`\n\n"
            f"📋 Tap to copy • Share this link to access all files",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Open Batch Link", url=link)]
            ])
        )
        
        # Clear batch session
        del batch_sessions[user_id]
        
    except Exception as e:
        logger.error(f"Error in handle_batch_done: {e}", exc_info=True)
        await callback.message.edit_text(f"❌ Error: {e}")


# ==================== STEP 5: RETRIEVE FILES - EXTRACT STORE ID AND SEND ====================

@app.on_message(filters.private & filters.command("start"))
async def handle_start(client: Client, message: Message):
    """Handle /start command and file/batch retrieval"""
    
    try:
        user_id = message.from_user.id
        
        # Check if there's a parameter (file_xxx or batch_xxx)
        if len(message.command) < 2:
            # Normal start message
            await message.reply_text(
                f"👋 **Welcome to File Store Bot!**\n\n"
                f"📤 Send me files to store and get shareable links.\n\n"
                f"🔗 Use links to access stored files anytime!"
            )
            return
        
        param = message.command[1]
        
        # Handle file retrieval: file_STOREID
        if param.startswith("file_"):
            store_id = param.replace("file_", "")
            await send_single_file(client, message, store_id, user_id)
            return
        
        # Handle batch retrieval: batch_STOREID
        if param.startswith("batch_"):
            store_id = param.replace("batch_", "")
            await send_batch_files(client, message, store_id, user_id)
            return
        
        await message.reply_text("❌ Invalid link format!")
        
    except Exception as e:
        logger.error(f"Error in handle_start: {e}", exc_info=True)
        await message.reply_text("❌ An error occurred!")


async def send_single_file(client: Client, message: Message, store_id: str, user_id: int):
    """Send a single file using store ID"""
    
    try:
        # Validate ObjectId format
        if not ObjectId.is_valid(store_id):
            await message.reply_text("❌ Invalid file ID!")
            return
        
        # Find file in database using store ID
        file_doc = await files_collection.find_one({"_id": ObjectId(store_id)})
        
        if not file_doc:
            await message.reply_text(
                "❌ **File Not Found!**\n\n"
                "This file may have been deleted or the link is invalid."
            )
            return
        
        status_msg = await message.reply_text("⏳ **Fetching your file...**")
        
        # Get channel info
        channel_id = file_doc.get("channel_id")
        channel_msg_id = file_doc.get("channel_msg_id")
        
        if not channel_id or not channel_msg_id:
            await status_msg.edit_text("❌ File storage information missing!")
            return
        
        try:
            # Get message from channel using channel msg ID
            file_message = await client.get_messages(channel_id, channel_msg_id)
            
            # Send file to user
            await file_message.copy(
                message.chat.id,
                caption=f"📁 **{file_doc['file_name']}**\n\n"
                        f"💾 Size: {format_size(file_doc['file_size'])}\n"
                        f"📝 Type: {file_doc['file_type'].upper()}\n\n"
                        f"{file_doc.get('caption', '') or ''}"
            )
            
            await status_msg.delete()
            
            # Log download
            await log_download(store_id, user_id, "single", 1)
            
            logger.info(f"File {store_id} sent to user {user_id}")
            
        except Exception as e:
            logger.error(f"Error fetching from channel: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **Unable to retrieve file**\n\n"
                f"Error: {e}"
            )
        
    except Exception as e:
        logger.error(f"Error in send_single_file: {e}", exc_info=True)
        await message.reply_text(f"❌ Error: {e}")


async def send_batch_files(client: Client, message: Message, batch_store_id: str, user_id: int):
    """Send all files from a batch using batch store ID"""
    
    try:
        # Validate ObjectId format
        if not ObjectId.is_valid(batch_store_id):
            await message.reply_text("❌ Invalid batch ID!")
            return
        
        # Find batch in database using batch store ID
        batch_doc = await batches_collection.find_one({"_id": ObjectId(batch_store_id)})
        
        if not batch_doc:
            await message.reply_text(
                "❌ **Batch Not Found!**\n\n"
                "This batch may have been deleted or the link is invalid."
            )
            return
        
        file_count = batch_doc["file_count"]
        total_size = format_size(batch_doc["total_size"])
        
        # Send batch info
        files_list = "\n".join([
            f"{i+1}. 📁 {f['name']}"
            for i, f in enumerate(batch_doc["files_info"])
        ])
        
        await message.reply_text(
            f"📦 **Batch Files**\n\n"
            f"📊 Total Files: {file_count}\n"
            f"💾 Total Size: {total_size}\n\n"
            f"**Files:**\n{files_list}\n\n"
            f"⏳ Sending files..."
        )
        
        sent_count = 0
        failed_count = 0
        
        # Get all file store IDs from batch
        file_store_ids = batch_doc.get("file_store_ids", [])
        
        for file_store_id in file_store_ids:
            try:
                # Get file document using store ID
                file_doc = await files_collection.find_one({"_id": ObjectId(file_store_id)})
                
                if not file_doc:
                    failed_count += 1
                    continue
                
                # Get file from channel
                channel_id = file_doc.get("channel_id")
                channel_msg_id = file_doc.get("channel_msg_id")
                
                if not channel_id or not channel_msg_id:
                    failed_count += 1
                    continue
                
                file_message = await client.get_messages(channel_id, channel_msg_id)
                
                # Send file to user
                await file_message.copy(
                    message.chat.id,
                    caption=f"📁 {file_doc['file_name']}\n"
                            f"💾 {format_size(file_doc['file_size'])}"
                )
                
                sent_count += 1
                await asyncio.sleep(0.5)  # Small delay
                
            except Exception as e:
                logger.error(f"Error sending file {file_store_id}: {e}", exc_info=True)
                failed_count += 1
        
        # Send completion message
        status_text = f"✅ **Batch Complete!**\n\n" \
                     f"📤 Sent: {sent_count}/{file_count} files"
        
        if failed_count > 0:
            status_text += f"\n❌ Failed: {failed_count} files"
        
        await message.reply_text(status_text)
        
        # Log download
        await log_download(batch_store_id, user_id, "batch", sent_count)
        
        logger.info(f"Batch {batch_store_id} sent to user {user_id}: {sent_count} files")
        
    except Exception as e:
        logger.error(f"Error in send_batch_files: {e}", exc_info=True)
        await message.reply_text(f"❌ Error: {e}")


async def log_download(store_id: str, user_id: int, download_type: str, file_count: int):
    """Log download activity"""
    
    log_entry = {
        "store_id": store_id,
        "user_id": user_id,
        "download_type": download_type,
        "file_count": file_count,
        "timestamp": datetime.utcnow()
    }
    
    try:
        await downloads_collection.insert_one(log_entry)
    except Exception as e:
        logger.error(f"Error logging download: {e}")


# ==================== CLEANUP OLD SESSIONS ====================

async def cleanup_expired_sessions():
    """Remove expired batch sessions after 1 hour"""
    while True:
        await asyncio.sleep(3600)  # Check every hour
        current_time = datetime.utcnow()
        
        expired_users = []
        for user_id, session in batch_sessions.items():
            time_diff = (current_time - session["timestamp"]).total_seconds()
            if time_diff > 3600:  # 1 hour
                expired_users.append(user_id)
        
        for user_id in expired_users:
            del batch_sessions[user_id]
            logger.info(f"Cleaned up expired session for user {user_id}")





@Client.on_message(filters.command("start") & filters.incoming)
async def start(client, message):
    if EMOJI_MODE:
        try:
            await message.react(emoji=random.choice(REACTIONS), big=True)
        except Exception:
            await message.react(emoji="⚡️", big=True)
    
    m = message
    user_id = message.from_user.id
    
    # Handle file storage system commands first
    if len(message.command) >= 2:
        param = message.command[1]
        
        # Check if it's a file request
        if param.startswith("file_"):
            file_id = param.replace("file_", "")
            return await send_single_file(client, message, file_id, user_id)
        
        # Check if it's a batch request
        elif param.startswith("batch_"):
            batch_id = param.replace("batch_", "")
            return await send_batch_files(client, message, batch_id, user_id)
    
    # Handle verification links (notcopy/sendall)
    if len(m.command) == 2 and m.command[1].startswith(('notcopy', 'sendall')):
        try:
            _, userid, verify_id, file_id = m.command[1].split("_", 3)
            user_id = int(userid)
        except (ValueError, IndexError):
            return await message.reply("<b>⚠️ Invalid verification link. Please try again.</b>")
        
        grp_id = temp.VERIFICATIONS.get(user_id, 0)
        settings = await get_settings(grp_id)         
        verify_id_info = await db.get_verify_id_info(user_id, verify_id)
        
        if not verify_id_info or verify_id_info["verified"]:
            return await message.reply("<b>ʟɪɴᴋ ᴇxᴘɪʀᴇᴅ ᴛʀʏ ᴀɢᴀɪɴ...</b>")  
        
        ist_timezone = pytz.timezone('Asia/Kolkata')
        if await db.user_verified(user_id):
            key = "third_time_verified"
        else:
            key = "second_time_verified" if await db.is_user_verified(user_id) else "last_verified"
        
        current_time = datetime.now(tz=ist_timezone)
        result = await db.update_notcopy_user(user_id, {key: current_time})
        await db.update_verify_id_info(user_id, verify_id, {"verified": True})
        
        if key == "third_time_verified": 
            num = 3 
            msg = script.THIRDT_VERIFY_COMPLETE_TEXT
        elif key == "second_time_verified":
            num = 2
            msg = script.SECOND_VERIFY_COMPLETE_TEXT
        else:
            num = 1
            msg = script.VERIFY_COMPLETE_TEXT
        
        if message.command[1].startswith('sendall'):
            verifiedfiles = f"https://telegram.me/{temp.U_NAME}?start=allfiles_{grp_id}_{file_id}"
        else:
            verifiedfiles = f"https://telegram.me/{temp.U_NAME}?start=file_{grp_id}_{file_id}"
        
        await client.send_message(
            settings['log'], 
            script.VERIFIED_LOG_TEXT.format(
                m.from_user.mention, 
                user_id, 
                datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%d %B %Y'), 
                num
            )
        )
        
        btn = [[
            InlineKeyboardButton("✅ ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ɢᴇᴛ ꜰɪʟᴇ ✅", url=verifiedfiles),
        ]]
        reply_markup = InlineKeyboardMarkup(btn)
        dlt = await m.reply_photo(
            photo=(VERIFY_IMG),
            caption=msg.format(message.from_user.mention, get_readable_time(TWO_VERIFY_GAP)),
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
        await asyncio.sleep(300)
        await dlt.delete()
        return         
    
    # Handle group messages
    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        buttons = [[
            InlineKeyboardButton('❤️ ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ ɢʀᴏᴜᴘ ❤️', url=f'http://t.me/{temp.U_NAME}?startgroup=true')
        ], [
            InlineKeyboardButton('🍁 Update Channel 🍁', url=UPDATE_CHNL_LNK)
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        await message.reply(
            script.GSTART_TXT.format(
                message.from_user.mention if message.from_user else message.chat.title, 
                temp.U_NAME, 
                temp.B_NAME
            ), 
            reply_markup=reply_markup, 
            disable_web_page_preview=True
        )
        await asyncio.sleep(2) 
        if not await db.get_chat(message.chat.id):
            total = await client.get_chat_members_count(message.chat.id)
            await client.send_message(
                LOG_CHANNEL, 
                script.LOG_TEXT_G.format(message.chat.title, message.chat.id, total, "Unknown")
            )       
            await db.add_chat(message.chat.id, message.chat.title)
        return 
    
    # Add user to database if not exists
    if not await db.is_user_exist(message.from_user.id):
        await db.add_user(message.from_user.id, message.from_user.first_name)
        await client.send_message(
            LOG_CHANNEL, 
            script.LOG_TEXT_P.format(message.from_user.id, message.from_user.mention)
        )
    
    # Handle basic /start command
    if len(message.command) != 2:
        buttons = [[
            InlineKeyboardButton('🔰 ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ ɢʀᴏᴜᴘ 🔰', url=f'http://t.me/{temp.U_NAME}?startgroup=true')
        ], [
            InlineKeyboardButton(' ʜᴇʟᴘ 📢', callback_data='help'),
            InlineKeyboardButton(' ᴀʙᴏᴜᴛ 📖', callback_data='about')
        ], [
            InlineKeyboardButton('ᴛᴏᴘ sᴇᴀʀᴄʜɪɴɢ ⭐', callback_data="topsearch"),
            InlineKeyboardButton('ᴜᴘɢʀᴀᴅᴇ 🎟', callback_data="premium_info"),
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        current_time = datetime.now(pytz.timezone(TIMEZONE))
        curr_time = current_time.hour        
        
        if curr_time < 12:
            gtxt = "ɢᴏᴏᴅ ᴍᴏʀɴɪɴɢ 🌞" 
        elif curr_time < 17:
            gtxt = "ɢᴏᴏᴅ ᴀғᴛᴇʀɴᴏᴏɴ 🌓" 
        elif curr_time < 21:
            gtxt = "ɢᴏᴏᴅ ᴇᴠᴇɴɪɴɢ 🌘"
        else:
            gtxt = "ɢᴏᴏᴅ ɴɪɢʜᴛ 🌑"
        
        m = await message.reply_text("⏳")
        await asyncio.sleep(0.4)
        await m.delete()        
        await message.reply_photo(
            photo=random.choice(PICS),
            caption=script.START_TXT.format(message.from_user.mention, gtxt, temp.U_NAME, temp.B_NAME),
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
        return

    # Handle special start commands
    if len(message.command) == 2 and message.command[1] in ["subscribe", "error", "okay", "help"]:
        buttons = [[
            InlineKeyboardButton('🔰 ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ ɢʀᴏᴜᴘ 🔰', url=f'http://t.me/{temp.U_NAME}?startgroup=true')
        ], [
            InlineKeyboardButton(' ʜᴇʟᴘ 📢', callback_data='help'),
            InlineKeyboardButton(' ᴀʙᴏᴜᴛ 📖', callback_data='about')
        ], [
            InlineKeyboardButton('ᴛᴏᴘ sᴇᴀʀᴄʜɪɴɢ ⭐', callback_data="topsearch"),
            InlineKeyboardButton('ᴜᴘɢʀᴀᴅᴇ 🎟', callback_data="premium_info"),
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        current_time = datetime.now(pytz.timezone(TIMEZONE))
        curr_time = current_time.hour        
        
        if curr_time < 12:
            gtxt = "ɢᴏᴏᴅ ᴍᴏʀɴɪɴɢ 🌞" 
        elif curr_time < 17:
            gtxt = "ɢᴏᴏᴅ ᴀғᴛᴇʀɴᴏᴏɴ 🌓" 
        elif curr_time < 21:
            gtxt = "ɢᴏᴏᴅ ᴇᴠᴇɴɪɴɢ 🌘"
        else:
            gtxt = "ɢᴏᴏᴅ ɴɪɢʜᴛ 🌑"
        
        m = await message.reply_text("⏳")
        await asyncio.sleep(0.4)
        await m.delete()        
        await message.reply_photo(
            photo=random.choice(PICS),
            caption=script.START_TXT.format(message.from_user.mention, gtxt, temp.U_NAME, temp.B_NAME),
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
        return
    
    # Handle referral links
    if message.command[1].startswith("reff_"):
        try:
            user_id = int(message.command[1].split("_")[1])
        except (ValueError, IndexError):
            await message.reply_text("Invalid refer!")
            return
        
        if user_id == message.from_user.id:
            await message.reply_text(
                "Hᴇʏ Dᴜᴅᴇ, Yᴏᴜ Cᴀɴ'ᴛ Rᴇғᴇʀ Yᴏᴜʀsᴇʟғ 🤣!\n\n"
                "sʜᴀʀᴇ ʟɪɴᴋ ʏᴏᴜʀ ғʀɪᴇɴᴅ ᴀɴᴅ ɢᴇᴛ 10 ʀᴇғᴇʀʀᴀʟ ᴘᴏɪɴᴛ ɪғ ʏᴏᴜ ᴀʀᴇ ᴄᴏʟʟᴇᴄᴛɪɴɢ 100 ʀᴇғᴇʀʀᴀʟ ᴘᴏɪɴᴛs "
                "ᴛʜᴇɴ ʏᴏᴜ ᴄᴀɴ ɢᴇᴛ 1 ᴍᴏɴᴛʜ ғʀᴇᴇ ᴘʀᴇᴍɪᴜᴍ ᴍᴇᴍʙᴇʀsʜɪᴘ."
            )
            return
        
        if referdb.is_user_in_list(message.from_user.id):
            await message.reply_text("Yᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴀʟʀᴇᴀᴅʏ ɪɴᴠɪᴛᴇᴅ ❗")
            return
        
        if await db.is_user_exist(message.from_user.id): 
            await message.reply_text("‼️ Yᴏᴜ Hᴀᴠᴇ Bᴇᴇɴ Aʟʀᴇᴀᴅʏ Iɴᴠɪᴛᴇᴅ ᴏʀ Jᴏɪɴᴇᴅ")
            return 
        
        try:
            uss = await client.get_users(user_id)
        except Exception:
            return
        
        referdb.add_user(message.from_user.id)
        fromuse = referdb.get_refer_points(user_id) + 10
        
        if fromuse == 100:
            referdb.add_refer_points(user_id, 0) 
            await message.reply_text(
                f"🎉 𝗖𝗼𝗻𝗴𝗿𝗮𝘁𝘂𝗹𝗮𝘁𝗶𝗼𝗻𝘀! 𝗬𝗼𝘂 𝘄𝗼𝗻 𝟭𝟬 𝗥𝗲𝗳𝗲𝗿𝗿𝗮𝗹 𝗽𝗼𝗶𝗻𝘁 𝗯𝗲𝗰𝗮𝘂𝘀𝗲 "
                f"𝗬𝗼𝘂 𝗵𝗮𝘃𝗲 𝗯𝗲𝗲𝗻 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆 𝗜𝗻𝘃𝗶𝘁𝗲𝗱 ☞ {uss.mention}!"
            )
            await client.send_message(
                user_id, 
                f"You have been successfully invited by {message.from_user.mention}!"
            )
            
            seconds = 2592000
            if seconds > 0:
                expiry_time = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
                user_data = {"id": user_id, "expiry_time": expiry_time}
                await db.update_user(user_data)
                await client.send_message(
                    chat_id=user_id,
                    text=f"<b>Hᴇʏ {uss.mention}\n\nYᴏᴜ ɢᴏᴛ 1 ᴍᴏɴᴛʜ ᴘʀᴇᴍɪᴜᴍ sᴜʙsᴄʀɪᴘᴛɪᴏɴ ʙʏ ɪɴᴠɪᴛɪɴɢ 10 ᴜsᴇʀs ❗</b>",
                    disable_web_page_preview=True              
                )
            
            for admin in ADMINS:
                await client.send_message(
                    chat_id=admin, 
                    text=f"Sᴜᴄᴄᴇss ғᴜʟʟʏ ᴛᴀsᴋ ᴄᴏᴍᴘʟᴇᴛᴇᴅ ʙʏ ᴛʜɪs ᴜsᴇʀ:\n\n"
                         f"user Nᴀᴍᴇ: {uss.mention}\n\nUsᴇʀ ɪᴅ: {uss.id}!"
                )
        else:
            referdb.add_refer_points(user_id, fromuse)
            await message.reply_text(f"You have been successfully invited by {uss.mention}!")
            await client.send_message(
                user_id, 
                f"𝗖𝗼𝗻𝗴𝗿𝗮𝘁𝘂𝗹𝗮𝘁𝗶𝗼𝗻𝘀! 𝗬𝗼𝘂 𝘄𝗼𝗻 𝟭𝟬 𝗥𝗲𝗳𝗲𝗿𝗿𝗮𝗹 𝗽𝗼𝗶𝗻𝘁 𝗯𝗲𝗰𝗮𝘂𝘀𝗲 "
                f"𝗬𝗼𝘂 𝗵𝗮𝘃𝗲 𝗯𝗲𝗲𝗻 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆 𝗜𝗻𝘃𝗶𝘁𝗲𝗱 ☞{message.from_user.mention}!"
            )
        return
    
    # Handle premium command
    if len(message.command) == 2 and message.command[1] in ["premium"]:
        buttons = [[
            InlineKeyboardButton('📲 ꜱᴇɴᴅ ᴘᴀʏᴍᴇɴᴛ ꜱᴄʀᴇᴇɴꜱʜᴏᴛ', url=OWNER_LNK)
        ], [
            InlineKeyboardButton('❌ ᴄʟᴏꜱᴇ ❌', callback_data='close_data')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        await message.reply_photo(
            photo=(SUBSCRIPTION),
            caption=script.PREPLANS_TXT.format(message.from_user.mention, OWNER_UPI_ID, QR_CODE),
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
        return  
    
    # Handle getfile command
    if len(message.command) == 2 and message.command[1].startswith('getfile'):
        movies = message.command[1].split("-", 1)[1] 
        movie = movies.replace('-', ' ')
        message.text = movie 
        await auto_filter(client, message) 
        return
    
    # Parse file data
    data = message.command[1]
    try:
        parts = data.split("_", 2)
        if len(parts) >= 3:
            _, grp_id, file_id = parts
            grp_id = int(grp_id)
        else:
            _, grp_id, file_id = "", 0, data
    except (ValueError, IndexError):
        _, grp_id, file_id = "", 0, data

    # Fetch file details concurrently
    file_details_task = asyncio.create_task(get_file_details(file_id))

    # Force subscription check
    if not await db.has_premium_access(message.from_user.id): 
        try:
            btn = []
            # Only parse chat ID if data contains underscores
            if "_" in data and len(data.split("_", 2)) >= 2:
                try:
                    chat = int(data.split("_", 2)[1])
                    settings = await get_settings(chat)
                    fsub_channels = list(dict.fromkeys((settings.get('fsub', []) if settings else []) + AUTH_CHANNELS)) 

                    if fsub_channels:
                        btn += await is_subscribed(client, message.from_user.id, fsub_channels)
                    if AUTH_REQ_CHANNELS:
                        btn += await is_req_subscribed(client, message.from_user.id, AUTH_REQ_CHANNELS)
                    
                    if btn:
                        if len(message.command) > 1 and "_" in message.command[1]:
                            kk, file_id = message.command[1].split("_", 1)
                            btn.append([
                                InlineKeyboardButton("♻️ ᴛʀʏ ᴀɢᴀɪɴ ♻️", callback_data=f"checksub#{kk}#{file_id}")
                            ])
                        reply_markup = InlineKeyboardMarkup(btn)
                        photo = random.choice(FSUB_PICS) if FSUB_PICS else "https://graph.org/file/7478ff3eac37f4329c3d8.jpg"
                        caption = (
                            f"👋 ʜᴇʟʟᴏ {message.from_user.mention}\n\n"
                            "🛑 ʏᴏᴜ ᴍᴜsᴛ ᴊᴏɪɴ ᴛʜᴇ ʀᴇǫᴜɪʀᴇᴅ ᴄʜᴀɴɴᴇʟs ᴛᴏ ᴄᴏɴᴛɪɴᴜᴇ.\n"
                            "👉 ᴊᴏɪɴ ᴀʟʟ ᴛʜᴇ ʙᴇʟᴏᴡ ᴄʜᴀɴɴᴇʟs ᴀɴᴅ ᴛʀʏ ᴀɢᴀɪɴ."
                        )
                        await message.reply_photo(
                            photo=photo,
                            caption=caption,
                            reply_markup=reply_markup,
                            parse_mode=enums.ParseMode.HTML
                        )
                        return
                except (ValueError, IndexError):
                    pass  # Continue if parsing fails
        except Exception as e:
            await log_error(client, f"❗️ Force Sub Error:\n\n{repr(e)}")
            logger.error(f"❗️ Force Sub Error:\n\n{repr(e)}")

    # Verification check
    user_id = m.from_user.id
    if not await db.has_premium_access(user_id):
        try:
            grp_id = int(grp_id) if grp_id else 0
            user_verified = await db.is_user_verified(user_id)
            settings = await get_settings(grp_id)
            is_second_shortener = await db.use_second_shortener(user_id, settings.get('verify_time', TWO_VERIFY_GAP)) 
            is_third_shortener = await db.use_third_shortener(user_id, settings.get('third_verify_time', THREE_VERIFY_GAP))
            
            if settings.get("is_verify", IS_VERIFY) and (not user_verified or is_second_shortener or is_third_shortener):
                verify_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
                await db.create_verify_id(user_id, verify_id)
                temp.VERIFICATIONS[user_id] = grp_id
                
                if message.command[1].startswith('allfiles'):
                    verify = await get_shortlink(
                        f"https://telegram.me/{temp.U_NAME}?start=sendall_{user_id}_{verify_id}_{file_id}", 
                        grp_id, is_second_shortener, is_third_shortener
                    )
                else:
                    verify = await get_shortlink(
                        f"https://telegram.me/{temp.U_NAME}?start=notcopy_{user_id}_{verify_id}_{file_id}", 
                        grp_id, is_second_shortener, is_third_shortener
                    )
                
                if is_third_shortener:
                    howtodownload = settings.get('tutorial_3', TUTORIAL_3)
                else:
                    howtodownload = settings.get('tutorial_2', TUTORIAL_2) if is_second_shortener else settings.get('tutorial', TUTORIAL)
                
                buttons = [[
                    InlineKeyboardButton(text="♻️ ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ᴠᴇʀɪꜰʏ ♻️", url=verify)
                ], [
                    InlineKeyboardButton(text="⁉️ ʜᴏᴡ ᴛᴏ ᴠᴇʀɪꜰʏ ⁉️", url=howtodownload)
                ]]
                reply_markup = InlineKeyboardMarkup(buttons)
                
                if await db.user_verified(user_id): 
                    msg = script.THIRDT_VERIFICATION_TEXT
                else:            
                    msg = script.SECOND_VERIFICATION_TEXT if is_second_shortener else script.VERIFICATION_TEXT
                
                n = await m.reply_text(
                    text=msg.format(message.from_user.mention),
                    protect_content=True,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML
                )
                await asyncio.sleep(300) 
                await n.delete()
                await m.delete()
                return
        except Exception as e:
            print(f"Error In Verification - {e}")
            pass

    # Await file details
    files_ = await file_details_task

    # Handle short link
    if data.startswith("short"): 
        try: 
            files_ = await get_file_details(file_id)
            files1 = files_[0]
            title = clean_filename(files1.file_name)
            size = get_size(files1.file_size)
            file_id = data.replace("short_", "file_", 1) 
            link = f"https://telegram.me/{temp.U_NAME}?start={file_id}"
            shortened_link = await get_shortlink(link)
            
            keyboard = [ 
                [InlineKeyboardButton("📥 Get File Here", url=shortened_link)], 
                [InlineKeyboardButton("❓ How to Download", url=TUTORIAL)] 
            ] 
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await message.reply_text(
                f"🎬 Name: {title}\nSize: {size}\n\nYour file is ready!\n\n"
                "Click the button below to get your file:", 
                reply_markup=reply_markup
            ) 
        except Exception as e: 
            print(f"Error processing file link: {e}") 
            await message.reply_text("Sorry, there was an error processing your request.")
        return

    # Handle allfiles
    if data.startswith("allfiles"):
        try:
            files = temp.GETALL.get(file_id)
            if not files:
                return await message.reply('<b><i>ɴᴏ ꜱᴜᴄʜ ꜰɪʟᴇ ᴇxɪꜱᴛꜱ !</b></i>')
            
            filesarr = []
            for file in files:
                # Handle both dict and object types
                file_id_value = file.get('file_id') if isinstance(file, dict) else file.file_id
                files_ = await get_file_details(file_id_value)
                if not files_:
                    continue
                    
                files1 = files_[0]
                # ... rest of the code remains same
                title = clean_filename(files1.file_name)
                size = get_size(files1.file_size)
                f_caption = files1.caption
                settings = await get_settings(int(grp_id))
                DREAMX_CAPTION = settings.get('caption', CUSTOM_FILE_CAPTION)
                
                if DREAMX_CAPTION:
                    try:
                        f_caption = DREAMX_CAPTION.format(
                            file_name='' if title is None else title, 
                            file_size='' if size is None else size, 
                            file_caption='' if f_caption is None else f_caption
                        )
                    except Exception as e:
                        logger.exception(e)
                        f_caption = f_caption
                
                if f_caption is None:
                    f_caption = f"{clean_filename(files1.file_name)}"
                
                # Determine button layout
                if STREAM_MODE and not PREMIUM_STREAM_MODE:
                    btn = [
                        [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'generate_stream_link:{file_id}')],
                        [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
                    ]
                elif STREAM_MODE and PREMIUM_STREAM_MODE:
                    if not await db.has_premium_access(message.from_user.id):
                        btn = [
                            [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'prestream')],
                            [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
                        ]
                    else:
                        btn = [
                            [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'generate_stream_link:{file_id}')],
                            [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
                        ]
                else:
                    btn = [[InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]]
                
                msg = await client.send_cached_media(
                    chat_id=message.from_user.id,
                    file_id=file_id,
                    caption=f_caption,
                    protect_content=settings.get('file_secure', PROTECT_CONTENT),
                    reply_markup=InlineKeyboardMarkup(btn)
                )
                filesarr.append(msg)
            
            k = await client.send_message(
                chat_id=message.from_user.id, 
                text=script.DEL_MSG.format(get_time(DELETE_TIME)), 
                parse_mode=enums.ParseMode.HTML
            )
            await asyncio.sleep(DELETE_TIME)
            for x in filesarr:
                await x.delete()
            await k.edit_text("<b>ʏᴏᴜʀ ᴀʟʟ ᴠɪᴅᴇᴏꜱ/ꜰɪʟᴇꜱ ᴀʀᴇ ᴅᴇʟᴇᴛᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ !\nᴋɪɴᴅʟʏ ꜱᴇᴀʀᴄʜ ᴀɢᴀɪɴ</b>")
            return
        except Exception as e:
            logger.exception(e)
            return

    # Handle files
    user = message.from_user.id
    settings = await get_settings(int(grp_id)) if grp_id else {}
    
    if not files_:
        pre, file_id = ((base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))).decode("ascii")).split("_", 1)
        try:
            if STREAM_MODE and not PREMIUM_STREAM_MODE:
                btn = [
                    [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'generate_stream_link:{file_id}')],
                    [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
                ]
            elif STREAM_MODE and PREMIUM_STREAM_MODE:
                if not await db.has_premium_access(message.from_user.id):
                    btn = [
                        [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'prestream')],
                        [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
                    ]
                else:
                    btn = [
                        [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'generate_stream_link:{file_id}')],
                        [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
                    ]
            else:
                btn = [[InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]]
            
            msg = await client.send_cached_media(
                chat_id=message.from_user.id,
                file_id=file_id,
                protect_content=settings.get('file_secure', PROTECT_CONTENT),
                reply_markup=InlineKeyboardMarkup(btn)
            )

            filetype = msg.media
            file = getattr(msg, filetype.value)
            title = clean_filename(file.file_name)
            size = get_size(file.file_size)
            f_caption = f"<code>{title}</code>"
            DREAMX_CAPTION = settings.get('caption', CUSTOM_FILE_CAPTION)
            
            if DREAMX_CAPTION:
                try:
                    f_caption = DREAMX_CAPTION.format(
                        file_name='' if title is None else title, 
                        file_size='' if size is None else size, 
                        file_caption=''
                    )
                except:
                    pass
            
            await msg.edit_caption(
                f_caption,
                reply_markup=InlineKeyboardMarkup(btn)
            )
            k = await msg.reply(
                script.DEL_MSG.format(get_time(DELETE_TIME)),
                quote=True, 
                parse_mode=enums.ParseMode.HTML
            )
            await asyncio.sleep(DELETE_TIME)
            await msg.delete()
            await k.edit_text("<b>ʏᴏᴜʀ ᴠɪᴅᴇᴏ / ꜰɪʟᴇ ɪꜱ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ !!</b>")
            return
        except Exception as e:
            logger.exception(e)
            pass
        return await message.reply('ɴᴏ ꜱᴜᴄʜ ꜰɪʟᴇ ᴇxɪꜱᴛꜱ !')
    
    files = files_[0]
    title = clean_filename(files.file_name)
    size = get_size(files.file_size)
    f_caption = files.caption
    DREAMX_CAPTION = settings.get('caption', CUSTOM_FILE_CAPTION)
    
    if DREAMX_CAPTION:
        try:
            f_caption = DREAMX_CAPTION.format(
                file_name='' if title is None else title, 
                file_size='' if size is None else size, 
                file_caption='' if f_caption is None else f_caption
            )
        except Exception as e:
            logger.exception(e)
            f_caption = f_caption

    if f_caption is None:
        f_caption = clean_filename(files.file_name)
    
    if STREAM_MODE and not PREMIUM_STREAM_MODE:
        btn = [
            [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'generate_stream_link:{file_id}')],
            [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
        ]
    elif STREAM_MODE and PREMIUM_STREAM_MODE:
        if not await db.has_premium_access(message.from_user.id):
            btn = [
                [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'prestream')],
                [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
            ]
        else:
            btn = [
                [InlineKeyboardButton('🚀 ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ / ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ 🖥️', callback_data=f'generate_stream_link:{file_id}')],
                [InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]
            ]
    else:
        btn = [[InlineKeyboardButton('📌 ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ 📌', url=UPDATE_CHNL_LNK)]]
    
    msg = await client.send_cached_media(
        chat_id=message.from_user.id,
        file_id=file_id,
        caption=f_caption,
        protect_content=settings.get('file_secure', PROTECT_CONTENT),
        reply_markup=InlineKeyboardMarkup(btn)
    )
    k = await msg.reply(
        script.DEL_MSG.format(get_time(DELETE_TIME)),
        quote=True, 
        parse_mode=enums.ParseMode.HTML
    )     
    await asyncio.sleep(DELETE_TIME)
    await msg.delete()
    await k.edit_text("<b>ʏᴏᴜʀ ᴠɪᴅᴇᴏ / ꜰɪʟᴇ ɪꜱ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ !!</b>")
    return
@Client.on_message(filters.command('logs') & filters.user(ADMINS))
async def log_file(bot, message):
    """Send log file"""
    try:
        await message.reply_document('DreamXlogs.txt', caption="📑 **ʟᴏɢꜱ**")
    except Exception as e:
        await message.reply(str(e))

@Client.on_message(filters.command('delete') & filters.user(ADMINS))
async def delete(bot, message):
    """Delete file from database"""
    reply = message.reply_to_message
    if reply and reply.media:
        msg = await message.reply("Pʀᴏᴄᴇssɪɴɢ...⏳", quote=True)
    else:
        await message.reply('Rᴇᴘʟʏ ᴛᴏ ғɪʟᴇ ᴡɪᴛʜ /delete ᴡʜɪᴄʜ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴅᴇʟᴇᴛᴇ', quote=True)
        return

    for file_type in ("document", "video", "audio"):
        media = getattr(reply, file_type, None)
        if media is not None:
            break
    else:
        await msg.edit('Tʜɪs ɪs ɴᴏᴛ sᴜᴘᴘᴏʀᴛᴇᴅ ғɪʟᴇ ғᴏʀᴍᴀᴛ')
        return
    
    file_id, file_ref = unpack_new_file_id(media.file_id)
    if await Media.count_documents({'file_id': file_id}):
        result = await Media.collection.delete_one({
            '_id': file_id,
        })
    else:
        result = await Media2.collection.delete_one({
            '_id': file_id,
        })
    if result.deleted_count:
        await msg.edit('Fɪʟᴇ ɪs sᴜᴄᴄᴇssғᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ ғʀᴏᴍ ᴅᴀᴛᴀʙᴀsᴇ ✅')
    else:
        file_name = re.sub(r"(_|\-|\.|\+)", " ", str(media.file_name))
        result = await Media.collection.delete_many({
            'file_name': file_name,
            'file_size': media.file_size,
            'mime_type': media.mime_type
            })
        if result.deleted_count:
            await msg.edit('Fɪʟᴇ ɪs sᴜᴄᴄᴇssғᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ ғʀᴏᴍ ᴅᴀᴛᴀʙᴀsᴇ ✅')
        else:
            result = await Media2.collection.delete_many({
                'file_name': file_name,
                'file_size': media.file_size,
                'mime_type': media.mime_type
            })
            if result.deleted_count:
                await msg.edit('Fɪʟᴇ ɪs sᴜᴄᴄᴇssғᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ ғʀᴏᴍ ᴅᴀᴛᴀʙᴀsᴇ')
            else:
                result = await Media.collection.delete_many({
                    'file_name': media.file_name,
                    'file_size': media.file_size,
                    'mime_type': media.mime_type
                })
                if result.deleted_count:
                    await msg.edit('Fɪʟᴇ ɪs sᴜᴄᴄᴇssғᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ ғʀᴏᴍ ᴅᴀᴛᴀʙᴀsᴇ ✅')
                else:
                    result = await Media2.collection.delete_many({
                        'file_name': media.file_name,
                        'file_size': media.file_size,
                        'mime_type': media.mime_type
                    })
                    if result.deleted_count:
                        await msg.edit('Fɪʟᴇ ɪs sᴜᴄᴄᴇssғᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ ғʀᴏᴍ ᴅᴀᴛᴀʙᴀsᴇ ✅')
                    else:
                        await msg.edit('Fɪʟᴇ ɴᴏᴛ ғᴏᴜɴᴅ ɪɴ ᴅᴀᴛᴀʙᴀsᴇ ❌')


@Client.on_message(filters.command('deleteall') & filters.user(ADMINS))
async def delete_all_index(bot, message):
    await message.reply_text(
        'ᴛʜɪꜱ ᴡɪʟʟ ᴅᴇʟᴇᴛᴇ ᴀʟʟ ʏᴏᴜʀ ɪɴᴅᴇxᴇᴅ ꜰɪʟᴇꜱ !\nᴅᴏ ʏᴏᴜ ꜱᴛɪʟʟ ᴡᴀɴᴛ ᴛᴏ ᴄᴏɴᴛɪɴᴜᴇ ?',
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="⚠️ ʏᴇꜱ ⚠️", callback_data="autofilter_delete"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ ɴᴏ ❌", callback_data="close_data"
                    )
                ],
            ]
        ),
        quote=True,
    )

@Client.on_message(filters.command('settings'))
async def settings(client, message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return await message.reply(f"ʏᴏᴜ'ʀᴇ ᴀɴᴏɴʏᴍᴏᴜꜱ ᴀᴅᴍɪɴ.")
    chat_type = message.chat.type
    if chat_type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        grp_id = message.chat.id
        if not await is_check_admin(client, grp_id, message.from_user.id):
            return await message.reply_text(script.NT_ADMIN_ALRT_TXT)
        await db.connect_group(grp_id, user_id)
        btn = [[
                InlineKeyboardButton("👤 ᴏᴘᴇɴ ɪɴ ᴘʀɪᴠᴀᴛᴇ ᴄʜᴀᴛ 👤", callback_data=f"opnsetpm#{grp_id}")
              ],[
                InlineKeyboardButton("👥 ᴏᴘᴇɴ ʜᴇʀᴇ 👥", callback_data=f"opnsetgrp#{grp_id}")
              ]]
        await message.reply_text(
                text="<b>ᴡʜᴇʀᴇ ᴅᴏ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴏᴘᴇɴ ꜱᴇᴛᴛɪɴɢꜱ ᴍᴇɴᴜ ? ⚙️</b>",
                reply_markup=InlineKeyboardMarkup(btn),
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML,
                reply_to_message_id=message.id
        )
    elif chat_type == enums.ChatType.PRIVATE:
        connected_groups = await db.get_connected_grps(user_id)
        if not connected_groups:
            return await message.reply_text("Nᴏ Cᴏɴɴᴇᴄᴛᴇᴅ Gʀᴏᴜᴘs Fᴏᴜɴᴅ .")
        group_list = []
        for group in connected_groups:
            try:
                Chat = await client.get_chat(group)
                group_list.append([ InlineKeyboardButton(text=Chat.title, callback_data=f"grp_pm#{Chat.id}") ])
            except Exception as e:
                print(f"Error In PM Settings Button - {e}")
                pass
        await message.reply_text(
                    "⚠️ ꜱᴇʟᴇᴄᴛ ᴛʜᴇ ɢʀᴏᴜᴘ ᴡʜᴏꜱᴇ ꜱᴇᴛᴛɪɴɢꜱ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴄʜᴀɴɢᴇ.\n\n"
                    "ɪꜰ ʏᴏᴜʀ ɢʀᴏᴜᴘ ɪꜱ ɴᴏᴛ ꜱʜᴏᴡɪɴɢ ʜᴇʀᴇ,\n"
                    "ᴜꜱᴇ /reload ɪɴ ᴛʜᴀᴛ ɢʀᴏᴜᴘ ᴀɴᴅ ɪᴛ ᴡɪʟʟ ᴀᴘᴘᴇᴀʀ ʜᴇʀᴇ.",
                    reply_markup=InlineKeyboardMarkup(group_list)
                )

@Client.on_message(filters.command('reload'))
async def connect_group(client, message):
    user_id = message.from_user.id
    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        await db.connect_group(message.chat.id, user_id)
        await message.reply_text("Gʀᴏᴜᴘ Rᴇʟᴏᴀᴅᴇᴅ ✅ Nᴏᴡ Yᴏᴜ Cᴀɴ Mᴀɴᴀɢᴇ Tʜɪs Gʀᴏᴜᴘ Fʀᴏᴍ PM.")
    elif message.chat.type == enums.ChatType.PRIVATE:
        if len(message.command) < 2:
            await message.reply_text("Example: /reload 123456789")
            return
        try:
            group_id = int(message.command[1])
            if not await is_check_admin(client, group_id, user_id):
                await message.reply_text(script.NT_ADMIN_ALRT_TXT)
                return
            chat = await client.get_chat(group_id)
            await db.connect_group(group_id, user_id)
            await message.reply_text(f"Lɪɴᴋᴇᴅ sᴜᴄᴄᴇssғᴜʟʟʏ ✅ {chat.title} ᴛᴏ PM.")
        except:
            await message.reply_text("Invalid group ID or error occurred.")

@Client.on_message(filters.command('set_template'))
async def save_template(client, message):
    sts = await message.reply("ᴄʜᴇᴄᴋɪɴɢ ᴛᴇᴍᴘʟᴀᴛᴇ...")
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return await message.reply("ʏᴏᴜ'ʀᴇ ᴀɴᴏɴʏᴍᴏᴜꜱ ᴀᴅᴍɪɴ.")

    if message.chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await sts.edit("⚠️ ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ɪɴ ᴀ ɢʀᴏᴜᴘ ᴄʜᴀᴛ.")

    group_id = message.chat.id
    title = message.chat.title
    if not await is_check_admin(client, group_id, user_id):
        await message.reply_text(script.NT_ADMIN_ALRT_TXT)
        return
    if len(message.command) < 2:
        return await sts.edit("⚠️ ɴᴏ ᴛᴇᴍᴘʟᴀᴛᴇ ᴘʀᴏᴠɪᴅᴇᴅ!")

    template = message.text.split(" ", 1)[1]
    await save_group_settings(group_id, 'template', template)
    await sts.edit(
        f"✅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴜᴘᴅᴀᴛᴇᴅ ᴛᴇᴍᴘʟᴀᴛᴇ ꜰᴏʀ <code>{title}</code> ᴛᴏ:\n\n{template}"
    )


# Must add REQST_CHANNEL and SUPPORT_CHAT_ID to use this feature
@Client.on_message((filters.command(["request", "Request"]) | filters.regex("#request") | filters.regex("#Request")) & filters.group)
async def requests(bot, message):
    if REQST_CHANNEL is None or SUPPORT_CHAT_ID is None: return
    if message.reply_to_message and SUPPORT_CHAT_ID == message.chat.id:
        chat_id = message.chat.id
        reporter = str(message.from_user.id)
        mention = message.from_user.mention
        success = True
        content = message.reply_to_message.text
        try:
            if REQST_CHANNEL is not None:
                btn = [[
                        InlineKeyboardButton('ᴠɪᴇᴡ ʀᴇǫᴜᴇꜱᴛ', url=f"{message.reply_to_message.link}"),
                        InlineKeyboardButton('ꜱʜᴏᴡ ᴏᴘᴛɪᴏɴꜱ', callback_data=f'show_option#{reporter}')
                      ]]
                reported_post = await bot.send_message(chat_id=REQST_CHANNEL, text=f"<b>📝 ʀᴇǫᴜᴇꜱᴛ : <u>{content}</u>\n\n📚 ʀᴇᴘᴏʀᴛᴇᴅ ʙʏ : {mention}\n📖 ʀᴇᴘᴏʀᴛᴇʀ ɪᴅ : {reporter}\n\n</b>", reply_markup=InlineKeyboardMarkup(btn))
                success = True
            elif len(content) >= 3:
                for admin in ADMINS:
                    btn = [[
                        InlineKeyboardButton('ᴠɪᴇᴡ ʀᴇǫᴜᴇꜱᴛ', url=f"{message.reply_to_message.link}"),
                        InlineKeyboardButton('ꜱʜᴏᴡ ᴏᴘᴛɪᴏɴꜱ', callback_data=f'show_option#{reporter}')
                      ]]
                    reported_post = await bot.send_message(chat_id=admin, text=f"<b>📝 ʀᴇǫᴜᴇꜱᴛ : <u>{content}</u>\n\n📚 ʀᴇᴘᴏʀᴛᴇᴅ ʙʏ : {mention}\n📖 ʀᴇᴘᴏʀᴛᴇʀ ɪᴅ : {reporter}\n\n</b>", reply_markup=InlineKeyboardMarkup(btn))
                    success = True
            else:
                if len(content) < 3:
                    await message.reply_text("<b>ʏᴏᴜ ᴍᴜꜱᴛ ᴛʏᴘᴇ ᴀʙᴏᴜᴛ ʏᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ [ᴍɪɴɪᴍᴜᴍ 3 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ]. ʀᴇǫᴜᴇꜱᴛꜱ ᴄᴀɴ'ᴛ ʙᴇ ᴇᴍᴘᴛʏ.</b>")
            if len(content) < 3:
                success = False
        except Exception as e:
            await message.reply_text(f"Error: {e}")
            pass
    elif SUPPORT_CHAT_ID == message.chat.id:
        chat_id = message.chat.id
        reporter = str(message.from_user.id)
        mention = message.from_user.mention
        success = True
        content = message.text
        keywords = ["#request", "/request", "#Request", "/Request"]
        for keyword in keywords:
            if keyword in content:
                content = content.replace(keyword, "")
        try:
            if REQST_CHANNEL is not None and len(content) >= 3:
                btn = [[
                        InlineKeyboardButton('ᴠɪᴇᴡ ʀᴇǫᴜᴇꜱᴛ', url=f"{message.link}"),
                        InlineKeyboardButton('ꜱʜᴏᴡ ᴏᴘᴛɪᴏɴꜱ', callback_data=f'show_option#{reporter}')
                      ]]
                reported_post = await bot.send_message(chat_id=REQST_CHANNEL, text=f"<b>📝 ʀᴇǫᴜᴇꜱᴛ : <u>{content}</u>\n\n📚 ʀᴇᴘᴏʀᴛᴇᴅ ʙʏ : {mention}\n📖 ʀᴇᴘᴏʀᴛᴇʀ ɪᴅ : {reporter}\n\n</b>", reply_markup=InlineKeyboardMarkup(btn))
                success = True
            elif len(content) >= 3:
                for admin in ADMINS:
                    btn = [[
                        InlineKeyboardButton('ᴠɪᴇᴡ ʀᴇǫᴜᴇꜱᴛ', url=f"{message.link}"),
                        InlineKeyboardButton('ꜱʜᴏᴡ ᴏᴘᴛɪᴏɴꜱ', callback_data=f'show_option#{reporter}')
                      ]]
                    reported_post = await bot.send_message(chat_id=admin, text=f"<b>📝 ʀᴇǫᴜᴇꜱᴛ : <u>{content}</u>\n\n📚 ʀᴇᴘᴏʀᴛᴇᴅ ʙʏ : {mention}\n📖 ʀᴇᴘᴏʀᴛᴇʀ ɪᴅ : {reporter}\n\n</b>", reply_markup=InlineKeyboardMarkup(btn))
                    success = True
            else:
                if len(content) < 3:
                    await message.reply_text("<b>ʏᴏᴜ ᴍᴜꜱᴛ ᴛʏᴘᴇ ᴀʙᴏᴜᴛ ʏᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ [ᴍɪɴɪᴍᴜᴍ 3 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ]. ʀᴇǫᴜᴇꜱᴛꜱ ᴄᴀɴ'ᴛ ʙᴇ ᴇᴍᴘᴛʏ.</b>")
            if len(content) < 3:
                success = False
        except Exception as e:
            await message.reply_text(f"Error: {e}")
            pass
    elif SUPPORT_CHAT_ID == message.chat.id:
        chat_id = message.chat.id
        reporter = str(message.from_user.id)
        mention = message.from_user.mention
        success = True
        content = message.text
        keywords = ["#request", "/request", "#Request", "/Request"]
        for keyword in keywords:
            if keyword in content:
                content = content.replace(keyword, "")
        try:
            if REQST_CHANNEL is not None and len(content) >= 3:
                btn = [[
                        InlineKeyboardButton('ᴠɪᴇᴡ ʀᴇǫᴜᴇꜱᴛ', url=f"{message.link}"),
                        InlineKeyboardButton('ꜱʜᴏᴡ ᴏᴘᴛɪᴏɴꜱ', callback_data=f'show_option#{reporter}')
                      ]]
                reported_post = await bot.send_message(chat_id=REQST_CHANNEL, text=f"<b>📝 ʀᴇǫᴜᴇꜱᴛ : <u>{content}</u>\n\n📚 ʀᴇᴘᴏʀᴛᴇᴅ ʙʏ : {mention}\n📖 ʀᴇᴘᴏʀᴛᴇʀ ɪᴅ : {reporter}\n\n</b>", reply_markup=InlineKeyboardMarkup(btn))
                success = True
            elif len(content) >= 3:
                for admin in ADMINS:
                    btn = [[
                        InlineKeyboardButton('ᴠɪᴇᴡ ʀᴇǫᴜᴇꜱᴛ', url=f"{message.link}"),
                        InlineKeyboardButton('ꜱʜᴏᴡ ᴏᴘᴛɪᴏɴꜱ', callback_data=f'show_option#{reporter}')
                      ]]
                    reported_post = await bot.send_message(chat_id=admin, text=f"<b>📝 ʀᴇǫᴜᴇꜱᴛ : <u>{content}</u>\n\n📚 ʀᴇᴘᴏʀᴛᴇᴅ ʙʏ : {mention}\n📖 ʀᴇᴘᴏʀᴛᴇʀ ɪᴅ : {reporter}\n\n</b>", reply_markup=InlineKeyboardMarkup(btn))
                    success = True
            else:
                if len(content) < 3:
                    await message.reply_text("<b>ʏᴏᴜ ᴍᴜꜱᴛ ᴛʏᴘᴇ ᴀʙᴏᴜᴛ ʏᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ [ᴍɪɴɪᴍᴜᴍ 3 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ]. ʀᴇǫᴜᴇꜱᴛꜱ ᴄᴀɴ'ᴛ ʙᴇ ᴇᴍᴘᴛʏ.</b>")
            if len(content) < 3:
                success = False
        except Exception as e:
            await message.reply_text(f"Error: {e}")
            pass
    else:
        success = False
    if success:
        '''if isinstance(REQST_CHANNEL, (int, str)):
            channels = [REQST_CHANNEL]
        elif isinstance(REQST_CHANNEL, list):
            channels = REQST_CHANNEL
        for channel in channels:
            chat = await bot.get_chat(channel)
        #chat = int(chat)'''
        link = await bot.create_chat_invite_link(int(REQST_CHANNEL))
        btn = [[
                InlineKeyboardButton('ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ', url=link.invite_link),
                InlineKeyboardButton('ᴠɪᴇᴡ ʀᴇǫᴜᴇꜱᴛ', url=f"{reported_post.link}")
              ]]
        await message.reply_text("<b>ʏᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ʜᴀꜱ ʙᴇᴇɴ ᴀᴅᴅᴇᴅ! ᴘʟᴇᴀꜱᴇ ᴡᴀɪᴛ ꜰᴏʀ ꜱᴏᴍᴇ ᴛɪᴍᴇ.\n\nᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ ꜰɪʀꜱᴛ & ᴠɪᴇᴡ ʀᴇǫᴜᴇꜱᴛ.</b>", reply_markup=InlineKeyboardMarkup(btn))

@Client.on_message(filters.command("send") & filters.user(ADMINS))
async def send_msg(bot, message):
    if message.reply_to_message:
        target_id = message.text.split(" ", 1)[1]
        out = "Users Saved In DB Are:\n\n"
        success = False
        try:
            user = await bot.get_users(target_id)
            users = await db.get_all_users()
            async for usr in users:
                out += f"{usr['id']}"
                out += '\n'
            if str(user.id) in str(out):
                await message.reply_to_message.copy(int(user.id))
                success = True
            else:
                success = False
            if success:
                await message.reply_text(f"<b>ʏᴏᴜʀ ᴍᴇꜱꜱᴀɢᴇ ʜᴀꜱ ʙᴇᴇɴ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ꜱᴇɴᴛ ᴛᴏ {user.mention}.</b>")
            else:
                await message.reply_text("<b>ᴛʜɪꜱ ᴜꜱᴇʀ ᴅɪᴅɴ'ᴛ ꜱᴛᴀʀᴛᴇᴅ ᴛʜɪꜱ ʙᴏᴛ ʏᴇᴛ !</b>")
        except Exception as e:
            await message.reply_text(f"<b>Error: {e}</b>")
    else:
        await message.reply_text("<b>ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ᴀꜱ ᴀ ʀᴇᴘʟʏ ᴛᴏ ᴀɴʏ ᴍᴇꜱꜱᴀɢᴇ ᴜꜱɪɴɢ ᴛʜᴇ ᴛᴀʀɢᴇᴛ ᴄʜᴀᴛ ɪᴅ. ꜰᴏʀ ᴇɢ:  /send ᴜꜱᴇʀɪᴅ</b>")

@Client.on_message(filters.command("deletefiles") & filters.user(ADMINS))
async def deletemultiplefiles(bot, message):
    chat_type = message.chat.type
    if chat_type != enums.ChatType.PRIVATE:
        return await message.reply_text(f"<b>Hey {message.from_user.mention}, This command won't work in groups. It only works on my PM !</b>")
    else:
        pass
    try:
        keyword = message.text.split(" ", 1)[1]
    except:
        return await message.reply_text(f"<b>Hey {message.from_user.mention}, Give me a keyword along with the command to delete files.</b>")
    k = await bot.send_message(chat_id=message.chat.id, text=f"<b>Fetching Files for your query {keyword} on DB... Please wait...</b>")
    files, total = await get_bad_files(keyword)
    total = len(files)
    if total == 0:
        await k.edit_text(f"<b>No files found for your query {keyword} !</b>")
        await asyncio.sleep(DELETE_TIME)
        await k.delete()
        return
    await k.delete()
    btn = [[
       InlineKeyboardButton("⚠️ Yes, Continue ! ⚠️", callback_data=f"killfilesdq#{keyword}")
       ],[
       InlineKeyboardButton("❌ No, Abort operation ! ❌", callback_data="close_data")
    ]]
    await message.reply_text(
        text=f"<b>Found {total} files for your query {keyword} !\n\nDo you want to delete?</b>",
        reply_markup=InlineKeyboardMarkup(btn),
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_callback_query(filters.regex("topsearch"))
async def topsearch_callback(client, callback_query):
    def is_alphanumeric(string):
        return bool(re.match('^[a-zA-Z0-9 ]*$', string))

    limit = 20
    top_messages = await mdb.get_top_messages(limit)
    seen_messages = set()
    truncated_messages = []
    for msg in top_messages:
        msg_lower = msg.lower()
        if msg_lower not in seen_messages and is_alphanumeric(msg):
            seen_messages.add(msg_lower)
            if len(msg) > 35:
                truncated_messages.append(msg[:32] + "...")
            else:
                truncated_messages.append(msg)
    keyboard = [truncated_messages[i:i+2] for i in range(0, len(truncated_messages), 2)]
    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        one_time_keyboard=True,
        resize_keyboard=True,
        placeholder="Most searches of the day"
    )
    await callback_query.message.reply_text(
        "<b>Tᴏᴘ Sᴇᴀʀᴄʜᴇs Oғ Tʜᴇ Dᴀʏ 👇</b>",
        reply_markup=reply_markup
    )
    await callback_query.answer()

@Client.on_message(filters.command('top_search'))
async def top(_, message):
    def is_alphanumeric(string):
        return bool(re.match('^[a-zA-Z0-9 ]*$', string))
    try:
        limit = int(message.command[1])
    except (IndexError, ValueError):
        limit = 20
    top_messages = await mdb.get_top_messages(limit)
    seen_messages = set()
    truncated_messages = []
    for msg in top_messages:
        msg_lower = msg.lower()
        if msg_lower not in seen_messages and is_alphanumeric(msg):
            seen_messages.add(msg_lower)
            if len(msg) > 35:
                truncated_messages.append(msg[:32] + "...")
            else:
                truncated_messages.append(msg)
    keyboard = [truncated_messages[i:i+2] for i in range(0, len(truncated_messages), 2)]
    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        one_time_keyboard=True,
        resize_keyboard=True,
        placeholder="Most searches of the day"
    )
    await message.reply_text(
        "<b>Tᴏᴘ Sᴇᴀʀᴄʜᴇs Oғ Tʜᴇ Dᴀʏ 👇</b>",
        reply_markup=reply_markup
    )

@Client.on_message(filters.command('trendlist'))
async def trendlist(client, message):
    def is_alphanumeric(string):
        return bool(re.match('^[a-zA-Z0-9 ]*$', string))
    limit = 31
    if len(message.command) > 1:
        try:
            limit = int(message.command[1])
        except ValueError:
            await message.reply_text(
                "Invalid number format.\nPlease provide a valid number after the /trendlist command."
            )
            return
    try:
        top_messages = await mdb.get_top_messages(limit)
    except Exception as e:
        await message.reply_text(f"Error retrieving messages: {str(e)}")
        return

    if not top_messages:
        await message.reply_text("No top messages found.")
        return
    seen_messages = set()
    truncated_messages = []

    for msg in top_messages:
        msg_lower = msg.lower()
        if msg_lower not in seen_messages and is_alphanumeric(msg):
            seen_messages.add(msg_lower)
            truncated_messages.append(msg[:32] + '...' if len(msg) > 35 else msg)

    if not truncated_messages:
        await message.reply_text("No valid top messages found.")
        return
    formatted_list = "\n".join([f"{i+1}. <b>{msg}</b>" for i, msg in enumerate(truncated_messages)])
    additional_message = (
        "⚡️ 𝑨𝒍𝒍 𝒕𝒉𝒆 𝒓𝒆𝒔𝒖𝒍𝒕𝒔 𝒂𝒃𝒐𝒗𝒆 𝒄𝒐𝒎𝒆 𝒇𝒓𝒐𝒎 𝒘𝒉𝒂𝒕 𝒖𝒔𝒆𝒓𝒔 𝒉𝒂𝒗𝒆 𝒔𝒆𝒂𝒓𝒄𝒉𝒆𝒅 𝒇𝒐𝒓. "
        "𝑻𝒉𝒆𝒚'𝒓𝒆 𝒔𝒉𝒐𝒘𝒏 𝒕𝒐 𝒚𝒐𝒖 𝒆𝒙𝒂𝒄𝒕𝒍𝒚 𝒂𝒔 𝒕𝒉𝒆𝒚 𝒘𝒆𝒓𝒆 𝒔𝒆𝒂𝒓𝒄𝒉𝒆𝒅, "
        "𝒘𝒊𝒕𝒉𝒐𝒖𝒕 𝒂𝒏𝒚 𝒄𝒉𝒂𝒏𝒈𝒆𝒔 𝒃𝒚 𝒕𝒉𝒆 𝒐𝒘𝒏𝒆𝒓."
    )
    formatted_list += f"\n\n{additional_message}"
    reply_text = f"<b>Top {len(truncated_messages)} Tʀᴀɴᴅɪɴɢ ᴏғ ᴛʜᴇ ᴅᴀʏ 👇:</b>\n\n{formatted_list}"
    await message.reply_text(reply_text)

@Client.on_message(filters.private & filters.command("pm_search") & filters.user(ADMINS))
async def set_pm_search(client, message):
    bot_id = client.me.id
    try:
        option = message.text.split(" ", 1)[1].strip().lower()
        enable_status = option in ['on', 'true']
    except (IndexError, ValueError):
        await message.reply_text("<b>💔 Invalid option. Please send 'on' or 'off' after the command..</b>")
        return
    try:
        await db.update_pm_search_status(bot_id, enable_status)
        response_text = (
            "<b> ᴘᴍ ꜱᴇᴀʀᴄʜ ᴇɴᴀʙʟᴇᴅ ✅</b>" if enable_status
            else "<b> ᴘᴍ ꜱᴇᴀʀᴄʜ ᴅɪꜱᴀʙʟᴇᴅ ❌</b>"
        )
        await message.reply_text(response_text)
    except Exception as e:
        logger.error(f"Error in set_pm_search: {e}")
        await message.reply_text(f"<b>❗ An error occurred: {e}</b>")

@Client.on_message(filters.private & filters.command("movie_update") & filters.user(ADMINS))
async def set_movie_update_notification(client, message):
    bot_id = client.me.id
    try:
        option = message.text.split(" ", 1)[1].strip().lower()
        enable_status = option in ['on', 'true']
    except (IndexError, ValueError):
        await message.reply_text("<b>💔 Invalid option. Please send 'on' or 'off' after the command.</b>")
        return
    try:
        await db.update_movie_update_status(bot_id, enable_status)
        response_text = (
            "<b>ᴍᴏᴠɪᴇ ᴜᴘᴅᴀᴛᴇ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴ ᴇɴᴀʙʟᴇᴅ ✅</b>" if enable_status
            else "<b>ᴍᴏᴠɪᴇ ᴜᴘᴅᴀᴛᴇ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴ ᴅɪꜱᴀʙʟᴇᴅ ❌</b>"
        )
        await message.reply_text(response_text)
    except Exception as e:
        logger.error(f"Error in set_movie_update_notification: {e}")
        await message.reply_text(f"<b>❗ An error occurred: {e}</b>")

@Client.on_message(filters.command("restart") & filters.user(ADMINS))
async def stop_button(bot, message):
    msg = await bot.send_message(text="<b><i>ʙᴏᴛ ɪꜱ ʀᴇꜱᴛᴀʀᴛɪɴɢ</i></b>", chat_id=message.chat.id)
    await asyncio.sleep(3)
    await msg.edit("<b><i><u>ʙᴏᴛ ɪꜱ ʀᴇꜱᴛᴀʀᴛᴇᴅ</u> ✅</i></b>")
    os.execl(sys.executable, sys.executable, *sys.argv)

@Client.on_message(filters.command("del_msg") & filters.user(ADMINS))
async def del_msg(client, message):
    confirm_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data="confirm_del_yes"),
        InlineKeyboardButton("No", callback_data="confirm_del_no")
    ]])
    sent_message = await message.reply_text(
        "⚠️ Aʀᴇ ʏᴏᴜ sᴜʀᴇ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴄʟᴇᴀʀ ᴛʜᴇ ᴜᴘᴅᴀᴛᴇs ᴄʜᴀɴɴᴇʟ ʟɪsᴛ ?\n\n ᴅᴏ ʏᴏᴜ ꜱᴛɪʟʟ ᴡᴀɴᴛ ᴛᴏ ᴄᴏɴᴛɪɴᴜᴇ ?",
        reply_markup=confirm_markup
    )
    await asyncio.sleep(60)
    try:
        await sent_message.delete()
    except Exception as e:
        print(f"Error deleting the message: {e}")

@Client.on_callback_query(filters.regex('^confirm_del_'))
async def confirmation_handler(client, callback_query):
    action = callback_query.data.split("_")[-1]
    if action == "yes":
        await db.delete_all_msg()
        await callback_query.message.edit_text('🧹 ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ ʟɪsᴛ ʜᴀs ʙᴇᴇɴ ᴄʟᴇᴀʀᴇᴅ sᴜᴄᴄᴇssғᴜʟʟʏ ✅')
    elif action == "no":
        await callback_query.message.delete()
    await callback_query.answer()

@Client.on_message(filters.command('set_caption'))
async def save_caption(client, message):
    grp_id = message.chat.id
    title = message.chat.title
    invite_link = await client.export_chat_invite_link(grp_id)
    if not await is_check_admin(client, grp_id, message.from_user.id):
        return await message.reply_text(script.NT_ADMIN_ALRT_TXT)
    chat_type = message.chat.type
    if chat_type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await message.reply_text("<b>ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ɪɴ ɢʀᴏᴜᴘ...</b>")
    try:
        caption = message.text.split(" ", 1)[1]
    except:
        return await message.reply_text("<code>ɢɪᴠᴇ ᴍᴇ ᴀ ᴄᴀᴘᴛɪᴏɴ ᴀʟᴏɴɢ ᴡɪᴛʜ ɪᴛ.\n\nᴇxᴀᴍᴘʟᴇ -\n\nꜰᴏʀ ꜰɪʟᴇ ɴᴀᴍᴇ ꜱᴇɴᴅ <code>{file_name}</code>\nꜰᴏʀ ꜰɪʟᴇ ꜱɪᴢᴇ ꜱᴇɴᴅ <code>{file_size}</code>\n\n<code>/set_caption {file_name}</code></code>")
    await save_group_settings(grp_id, 'caption', caption)
    await message.reply_text(f"ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴄʜᴀɴɢᴇᴅ ᴄᴀᴘᴛɪᴏɴ ꜰᴏʀ {title}\n\nᴄᴀᴘᴛɪᴏɴ - {caption}", disable_web_page_preview=True)
    await client.send_message(LOG_API_CHANNEL, f"#Set_Caption\n\nɢʀᴏᴜᴘ ɴᴀᴍᴇ : {title}\n\nɢʀᴏᴜᴘ ɪᴅ: {grp_id}\nɪɴᴠɪᴛᴇ ʟɪɴᴋ : {invite_link}\n\nᴜᴘᴅᴀᴛᴇᴅ ʙʏ : {message.from_user.username}")


@Client.on_message(filters.command(["set_tutorial", "set_tutorial_2", "set_tutorial_3"]))
async def set_tutorial(client, message: Message):
    grp_id = message.chat.id
    title = message.chat.title
    chat_type = message.chat.type
    if chat_type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await message.reply_text(
            f"<b>ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ɪɴ ɢʀᴏᴜᴘ...\n\nGroup Name: {title}\nGroup ID: {grp_id}</b>"
        )
    if not await is_check_admin(client, grp_id, message.from_user.id):
        return await message.reply_text(script.NT_ADMIN_ALRT_TXT)

    try:
        tutorial_link = message.text.split(" ", 1)[1]
    except IndexError:
        return await message.reply_text(
            f"<b>ᴄᴏᴍᴍᴀɴᴅ ɪɴᴄᴏᴍᴘʟᴇᴛᴇ !!\n\nᴜꜱᴇ ʟɪᴋᴇ ᴛʜɪꜱ -</b>\n\n"
            f"<code>/{message.command[0]} https://t.me/dreamxbotz</code>"
        )
    if message.command[0] == "set_tutorial":
        tutorial_key = "tutorial"
    else:
        tutorial_key = f"tutorial_{message.command[0].split('_', 2)[2]}"

    await save_group_settings(grp_id, tutorial_key, tutorial_link)
    invite_link = await client.export_chat_invite_link(grp_id)
    await message.reply_text(
        f"<b>ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴄʜᴀɴɢᴇᴅ {tutorial_key.replace('_', ' ').title()} ꜰᴏʀ {title}</b>\n\n"
        f"ʟɪɴᴋ - {tutorial_link}",
        disable_web_page_preview=True
    )
    await client.send_message(
        LOG_API_CHANNEL,
        f"#Set_{tutorial_key.title()}_Video\n\n"
        f"ɢʀᴏᴜᴘ ɴᴀᴍᴇ : {title}\n"
        f"ɢʀᴏᴜᴘ ɪᴅ : {grp_id}\n"
        f"ɪɴᴠɪᴛᴇ ʟɪɴᴋ : {invite_link}\n"
        f"ᴜᴘᴅᴀᴛᴇᴅ ʙʏ : {message.from_user.mention()}"
    )


async def handle_shortner_command(c, m, shortner_key, api_key, log_prefix, fallback_url, fallback_api):
    grp_id = m.chat.id
    if not await is_check_admin(c, grp_id, m.from_user.id):
        return await m.reply_text(script.NT_ADMIN_ALRT_TXT)
    if len(m.command) != 3:
        return await m.reply(
            f"<b>ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ʟɪᴋᴇ -\n\n`/{m.command[0]} omegalinks.in your_api_key_here`</b>"
        )
    sts = await m.reply("<b>♻️ ᴄʜᴇᴄᴋɪɴɢ...</b>")
    await asyncio.sleep(1.2)
    await sts.delete()
    if m.chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await m.reply_text("<b>ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ɪɴ ɢʀᴏᴜᴘ...</b>")
    try:
        URL = m.command[1]
        API = m.command[2]
        await save_group_settings(grp_id, shortner_key, URL)
        await save_group_settings(grp_id, api_key, API)
        await m.reply_text(f"<b><u>✅ sʜᴏʀᴛɴᴇʀ ᴀᴅᴅᴇᴅ</u>\n\nꜱɪᴛᴇ - `{URL}`\nᴀᴘɪ - `{API}`</b>")
        user_id = m.from_user.id
        user_info = f"@{m.from_user.username}" if m.from_user.username else f"{m.from_user.mention}"
        link = (await c.get_chat(m.chat.id)).invite_link
        grp_link = f"[{m.chat.title}]({link})"
        log_message = (
            f"#{log_prefix}\n\nɴᴀᴍᴇ - {user_info}\n\nɪᴅ - `{user_id}`"
            f"\n\nꜱɪᴛᴇ - {URL}\n\nᴀᴘɪ - `{API}`"
            f"\n\nɢʀᴏᴜᴘ - {grp_link}\nɢʀᴏᴜᴘ ɪᴅ - `{grp_id}`"
        )
        await c.send_message(LOG_API_CHANNEL, log_message, disable_web_page_preview=True)
    except Exception as e:
        await save_group_settings(grp_id, shortner_key, fallback_url)
        await save_group_settings(grp_id, api_key, fallback_api)
        await m.reply_text(
            f"<b><u>💢 ᴇʀʀᴏʀ ᴏᴄᴄᴜʀᴇᴅ!</u>\n\n"
            f"ᴅᴇꜰᴀᴜʟᴛ ꜱʜᴏʀᴛɴᴇʀ ᴀᴘᴘʟɪᴇᴅ\n"
            f"ɪꜰ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴄʜᴀɴɢᴇ ᴛʀʏ ᴀ ᴠᴀʟɪᴅ ꜱɪᴛᴇ ᴀɴᴅ ᴀᴘɪ ᴋᴇʏ.\n\n"
            f"ʟɪᴋᴇ:\n\n`/{m.command[0]} mdiskshortner.link your_api_key_here`\n\n"
            f"💔 ᴇʀʀᴏʀ - <code>{e}</code></b>"
        )

@Client.on_message(filters.command('set_shortner'))
async def set_shortner(c, m):
    await handle_shortner_command(c, m, 'shortner', 'api', 'New_Shortner_Set_For_1st_Verify', SHORTENER_WEBSITE, SHORTENER_API)

@Client.on_message(filters.command('set_shortner_2'))
async def set_shortner_2(c, m):
    await handle_shortner_command(c, m, 'shortner_two', 'api_two', 'New_Shortner_Set_For_2nd_Verify', SHORTENER_WEBSITE2, SHORTENER_API2)

@Client.on_message(filters.command('set_shortner_3'))
async def set_shortner_3(c, m):
    await handle_shortner_command(c, m, 'shortner_three', 'api_three', 'New_Shortner_Set_For_3rd_Verify', SHORTENER_WEBSITE3, SHORTENER_API3)

@Client.on_message(filters.command('set_log_channel'))
async def set_log(client, message):
    grp_id = message.chat.id
    title = message.chat.title
    if not await is_check_admin(client, grp_id, message.from_user.id):
        return await message.reply_text(script.NT_ADMIN_ALRT_TXT)
    if len(message.text.split()) == 1:
        await message.reply("<b>ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ʟɪᴋᴇ ᴛʜɪꜱ - \n\n`/set_log_channel -100******`</b>")
        return
    sts = await message.reply("<b>♻️ ᴄʜᴇᴄᴋɪɴɢ...</b>")
    await asyncio.sleep(1.2)
    await sts.delete()
    chat_type = message.chat.type
    if chat_type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await message.reply_text("<b>ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ɪɴ ɢʀᴏᴜᴘ...</b>")
    try:
        log = int(message.text.split(" ", 1)[1])
    except IndexError:
        return await message.reply_text("<b><u>ɪɴᴠᴀɪʟᴅ ꜰᴏʀᴍᴀᴛ!!</u>\n\nᴜsᴇ ʟɪᴋᴇ ᴛʜɪs - `/set_log_channel -100xxxxxxxx`</b>")
    except ValueError:
        return await message.reply_text('<b>ᴍᴀᴋᴇ sᴜʀᴇ ɪᴅ ɪs ɪɴᴛᴇɢᴇʀ...</b>')
    try:
        t = await client.send_message(chat_id=log, text="<b>ʜᴇʏ ᴡʜᴀᴛ's ᴜᴘ!!</b>")
        await asyncio.sleep(3)
        await t.delete()
    except Exception as e:
        return await message.reply_text(f'<b><u>😐 ᴍᴀᴋᴇ sᴜʀᴇ ᴛʜɪs ʙᴏᴛ ᴀᴅᴍɪɴ ɪɴ ᴛʜᴀᴛ ᴄʜᴀɴɴᴇʟ...</u>\n\n💔 ᴇʀʀᴏʀ - <code>{e}</code></b>')
    await save_group_settings(grp_id, 'log', log)
    await message.reply_text(f"<b>✅ sᴜᴄᴄᴇssꜰᴜʟʟʏ sᴇᴛ ʏᴏᴜʀ ʟᴏɢ ᴄʜᴀɴɴᴇʟ ꜰᴏʀ {title}\n\nɪᴅ - `{log}`</b>", disable_web_page_preview=True)
    user_id = message.from_user.id
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"{message.from_user.mention}"
    link = (await client.get_chat(message.chat.id)).invite_link
    grp_link = f"[{message.chat.title}]({link})"
    log_message = f"#New_Log_Channel_Set\n\nɴᴀᴍᴇ - {user_info}\n\nɪᴅ - `{user_id}`\n\nʟᴏɢ ᴄʜᴀɴɴᴇʟ ɪᴅ - `{log}`\nɢʀᴏᴜᴘ ʟɪɴᴋ - `{grp_link}`\n\nɢʀᴏᴜᴘ ɪᴅ : `{grp_id}`"
    await client.send_message(LOG_API_CHANNEL, log_message, disable_web_page_preview=True) 


@Client.on_message(filters.command('set_time'))
async def set_time(client, message):
    chat_type = message.chat.type
    if chat_type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await message.reply_text("<b>ᴜsᴇ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ɪɴ ɢʀᴏᴜᴘ...</b>")       
    grp_id = message.chat.id
    title = message.chat.title
    invite_link = await client.export_chat_invite_link(grp_id)
    if not await is_check_admin(client, grp_id, message.from_user.id):
        return await message.reply_text(script.NT_ADMIN_ALRT_TXT)
    try:
        time = int(message.text.split(" ", 1)[1])
    except:
        return await message.reply_text("<b>ᴄᴏᴍᴍᴀɴᴅ ɪɴᴄᴏᴍᴘʟᴇᴛᴇ\n\nᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ʟɪᴋᴇ ᴛʜɪꜱ - <code>/set_time 600</code> [ ᴛɪᴍᴇ ᴍᴜꜱᴛ ʙᴇ ɪɴ ꜱᴇᴄᴏɴᴅꜱ ]</b>")   
    await save_group_settings(grp_id, 'verify_time', time)
    await message.reply_text(f"<b>✅️ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ꜱᴇᴛ 2ɴᴅ ᴠᴇʀɪꜰʏ ᴛɪᴍᴇ ꜰᴏʀ {title}\n\nᴛɪᴍᴇ - <code>{time}</code></b>")
    await client.send_message(LOG_API_CHANNEL, f"#Set_2nd_Verify_Time\n\nɢʀᴏᴜᴘ ɴᴀᴍᴇ : {title}\n\nɢʀᴏᴜᴘ ɪᴅ : {grp_id}\n\nɪɴᴠɪᴛᴇ ʟɪɴᴋ : {invite_link}\n\nᴜᴘᴅᴀᴛᴇᴅ ʙʏ : {message.from_user.username}")

@Client.on_message(filters.command('set_time_2'))
async def set_time_2(client, message):
    chat_type = message.chat.type
    if chat_type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await message.reply_text("<b>ᴜsᴇ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ɪɴ ɢʀᴏᴜᴘ...</b>")       
    grp_id = message.chat.id
    title = message.chat.title
    invite_link = await client.export_chat_invite_link(grp_id)
    if not await is_check_admin(client, grp_id, message.from_user.id):
        return await message.reply_text(script.NT_ADMIN_ALRT_TXT)
    try:
        time = int(message.text.split(" ", 1)[1])
    except:
        return await message.reply_text("<b>ᴄᴏᴍᴍᴀɴᴅ ɪɴᴄᴏᴍᴘʟᴇᴛᴇ\n\nᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ ʟɪᴋᴇ ᴛʜɪꜱ - <code>/set_time 3600</code> [ ᴛɪᴍᴇ ᴍᴜꜱᴛ ʙᴇ ɪɴ ꜱᴇᴄᴏɴᴅꜱ ]</b>")   
    await save_group_settings(grp_id, 'third_verify_time', time)
    await message.reply_text(f"<b>✅️ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ꜱᴇᴛ 3ʀᴅ ᴠᴇʀɪꜰʏ ᴛɪᴍᴇ ꜰᴏʀ {title}\n\nᴛɪᴍᴇ - <code>{time}</code></b>")
    await client.send_message(LOG_API_CHANNEL, f"#Set_3rd_Verify_Time\n\nɢʀᴏᴜᴘ ɴᴀᴍᴇ : {title}\n\nɢʀᴏᴜᴘ ɪᴅ : {grp_id}\n\nɪɴᴠɪᴛᴇ ʟɪɴᴋ : {invite_link}\n\nᴜᴘᴅᴀᴛᴇᴅ ʙʏ : {message.from_user.username}")


@Client.on_message(filters.command('details'))
async def all_settings(client, message):
    if message.chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await message.reply_text("<b>ᴜsᴇ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ɪɴ ɢʀᴏᴜᴘ...</b>")
    grp_id = message.chat.id
    title = message.chat.title
    if not await is_check_admin(client, grp_id, message.from_user.id):
        return await message.reply_text(script.NT_ADMIN_ALRT_TXT)
    try:
        settings = await get_settings(grp_id)
    except Exception as e:
        return await message.reply_text(f"<b>⚠️ ᴇʀʀᴏʀ ꜰᴇᴛᴄʜɪɴɢ ꜱᴇᴛᴛɪɴɢꜱ:</b>\n<code>{e}</code>")
    text = generate_settings_text(settings, title)
    btn = [
        [InlineKeyboardButton("♻️ ʀᴇꜱᴇᴛ ꜱᴇᴛᴛɪɴɢꜱ", callback_data=f"reset_group_{grp_id}")],
        [InlineKeyboardButton("🚫 ᴄʟᴏꜱᴇ", callback_data="close_data")]
    ]
    dlt = await message.reply_text(text, reply_markup=InlineKeyboardMarkup(btn), disable_web_page_preview=True)
    await asyncio.sleep(300)
    await dlt.delete()

@Client.on_callback_query(filters.regex(r"^reset_group_(\-\d+)$"))
async def reset_group_callback(client, callback_query):
    grp_id = int(callback_query.matches[0].group(1))
    user_id = callback_query.from_user.id
    if not await is_check_admin(client, grp_id, user_id):
        return await callback_query.answer(script.NT_ADMIN_ALRT_TXT, show_alert=True)
    await callback_query.answer("♻️ ʀᴇꜱᴇᴛᴛɪɴɢ ꜱᴇᴛᴛɪɴɢꜱ...")
    defaults = {
        'shortner': SHORTENER_WEBSITE,
        'api': SHORTENER_API,
        'shortner_two': SHORTENER_WEBSITE2,
        'api_two': SHORTENER_API2,
        'shortner_three': SHORTENER_WEBSITE3,
        'api_three': SHORTENER_API3,
        'verify_time': TWO_VERIFY_GAP,
        'third_verify_time': THREE_VERIFY_GAP,
        'template': IMDB_TEMPLATE,
        'tutorial': TUTORIAL,
        'tutorial_2': TUTORIAL_2,
        'tutorial_3': TUTORIAL_3,
        'caption': CUSTOM_FILE_CAPTION,
        'log': LOG_CHANNEL,
        'is_verify': IS_VERIFY,
        'fsub': AUTH_CHANNELS
    }
    current = await get_settings(grp_id)
    if current == defaults:
        return await callback_query.answer("✅ ꜱᴇᴛᴛɪɴɢꜱ ᴀʟʀᴇᴀᴅʏ ᴅᴇꜰᴀᴜʟᴛ.", show_alert=True)
    for key, value in defaults.items():
        await save_group_settings(grp_id, key, value)
    updated = await get_settings(grp_id)
    title = callback_query.message.chat.title
    text = generate_settings_text(updated, title, reset_done=True)
    buttons = [
        [InlineKeyboardButton("♻️ ʀᴇꜱᴇᴛ ꜱᴇᴛᴛɪɴɢꜱ", callback_data=f"reset_group_{grp_id}")],
        [InlineKeyboardButton("🚫 ᴄʟᴏꜱᴇ", callback_data="close_data")]
    ]
    await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)

@Client.on_message(filters.command("verify") & filters.user(ADMINS))
async def verify(bot, message):
    try:
        chat_type = message.chat.type
        if chat_type == enums.ChatType.PRIVATE:
            return await message.reply_text("ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴡᴏʀᴋs ᴏɴʟʏ ɪɴ ɢʀᴏᴜᴘs!")
        if chat_type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            grpid = message.chat.id
            title = message.chat.title
            command_text = message.text.split(' ')[1] if len(message.text.split(' ')) > 1 else None
            if command_text == "off":
                await save_group_settings(grpid, 'is_verify', False)
                return await message.reply_text("✓ ᴠᴇʀɪꜰʏ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅɪsᴀʙʟᴇᴅ.")
            elif command_text == "on":
                await save_group_settings(grpid, 'is_verify', True)
                return await message.reply_text("✗ ᴠᴇʀɪꜰʏ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴇɴᴀʙʟᴇᴅ.")
            else:
                return await message.reply_text("ʜɪ, ᴛᴏ ᴇɴᴀʙʟᴇ ᴠᴇʀɪꜰʏ, ᴜsᴇ <code>/verify on</code> ᴀɴᴅ ᴛᴏ ᴅɪsᴀʙʟᴇ ᴠᴇʀɪꜰʏ, ᴜsᴇ <code>/verify off</code>.")
    except Exception as e:
        print(f"Error: {e}")
        await message.reply_text(f"Error: {e}")

@Client.on_message(filters.command('set_fsub'))
async def set_fsub(client, message):
    try:
        userid = message.from_user.id if message.from_user else None
        if not userid:
            return await message.reply("<b>You are Anonymous admin you can't use this command !</b>")
        if message.chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            return await message.reply_text("ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴄᴀɴ ᴏɴʟʏ ʙᴇ ᴜsᴇᴅ ɪɴ ɢʀᴏᴜᴘs")
        grp_id = message.chat.id
        title = message.chat.title
        if not await is_check_admin(client, grp_id, userid):
            return await message.reply_text(script.NT_ADMIN_ALRT_TXT)
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            return await message.reply_text(
                "ᴄᴏᴍᴍᴀɴᴅ ɪɴᴄᴏᴍᴘʟᴇᴛᴇ!\n\n"
                "ᴄᴀɴ ᴀᴅᴅ ᴍᴜʟᴛɪᴘʟᴇ ᴄʜᴀɴɴᴇʟs sᴇᴘᴀʀᴀᴛᴇᴅ ʙʏ sᴘᴀᴄᴇs. ʟɪᴋᴇ: /sᴇᴛ_ғsᴜʙ ɪᴅ1 ɪᴅ2 ɪᴅ3\n"
            )
        option = args[1].strip()
        try:
            fsub_ids = [int(x) for x in option.split()]
        except ValueError:
            return await message.reply_text('ᴍᴀᴋᴇ sᴜʀᴇ ᴀʟʟ ɪᴅs ᴀʀᴇ ɪɴᴛᴇɢᴇʀs.')
        if len(fsub_ids) > 5:
            return await message.reply_text("ᴍᴀxɪᴍᴜᴍ 5 ᴄʜᴀɴɴᴇʟs ᴀʟʟᴏᴡᴇᴅ.")
        channels = "ᴄʜᴀɴɴᴇʟs:\n"
        channel_titles = []
        for id in fsub_ids:
            try:
                chat = await client.get_chat(id)
            except Exception as e:
                return await message.reply_text(
                    f"{id} ɪs ɪɴᴠᴀʟɪᴅ!\nᴍᴀᴋᴇ sᴜʀᴇ ᴛʜɪs ʙᴏᴛ ɪs ᴀᴅᴍɪɴ ɪɴ ᴛʜᴀᴛ ᴄʜᴀɴɴᴇʟ.\n\nError - {e}"
                )
            if chat.type != enums.ChatType.CHANNEL:
                return await message.reply_text(f"{id} ɪs ɴᴏᴛ ᴀ ᴄʜᴀɴɴᴇʟ.")
            channel_titles.append(f"{chat.title} (`{id}`)")
            channels += f'{chat.title}\n'
        await save_group_settings(grp_id, 'fsub', fsub_ids)
        await message.reply_text(f"sᴜᴄᴄᴇssғᴜʟʟʏ sᴇᴛ ꜰꜱᴜʙ ᴄʜᴀɴɴᴇʟ(ꜱ) ғᴏʀ {title} ᴛᴏ\n\n{channels}")
        mention = message.from_user.mention if message.from_user else "Unknown"
        await client.send_message(
            LOG_API_CHANNEL,
            f"#Fsub_Channel_set\n\n"
            f"ᴜꜱᴇʀ - {mention} ꜱᴇᴛ ᴛʜᴇ ꜰᴏʀᴄᴇ ᴄʜᴀɴɴᴇʟ(ꜱ) ꜰᴏʀ {title}:\n\n"
            f"ꜰꜱᴜʙ ᴄʜᴀɴɴᴇʟ(ꜱ):\n" + '\n'.join(channel_titles)
        )
    except Exception as e:
        err_text = f"⚠️ Error in set_fSub :\n{e}"
        logger.error(err_text)
        await client.send_message(LOG_API_CHANNEL, err_text)

@Client.on_message(filters.private & filters.command("resetallgroup") & filters.user(ADMINS))
async def reset_all_settings(client, message):
    try:
        reset_count = await db.dreamx_reset_settings()
        await message.reply_text(
            f"<b>ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ ꜱᴇᴛᴛɪɴɢꜱ ꜰᴏʀ  <code>{reset_count}</code> ɢʀᴏᴜᴘꜱ. ᴅᴇꜰᴀᴜʟᴛ ᴠᴀʟᴜᴇꜱ ᴡɪʟʟ ʙᴇ ᴜꜱᴇᴅ ✅</b>",
            quote=True
        )
    except Exception as e:
        print(f"[ERROR] reset_all_settings: {e}")
        await message.reply_text(
            "<b>🚫 An error occurred while resetting group settings.\nPlease try again later.</b>",
            quote=True
        )

@Client.on_message(filters.command("trial_reset"))
async def reset_trial(client, message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.reply("ʏᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ᴀɴʏ ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴛᴏ ᴜꜱᴇ ᴛʜɪꜱ ᴄᴏᴍᴍᴀɴᴅ.")
        return
    try:
        if len(message.command) > 1:
            target_user_id = int(message.command[1])
            updated_count = await db.reset_free_trial(target_user_id)
            message_text = f"ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ʀᴇꜱᴇᴛ ꜰʀᴇᴇ ᴛʀᴀɪʟ ꜰᴏʀ ᴜꜱᴇʀꜱ {target_user_id}." if updated_count else f"ᴜꜱᴇʀ {target_user_id} ɴᴏᴛ ꜰᴏᴜɴᴅ ᴏʀ ᴅᴏɴ'ᴛ ᴄʟᴀɪᴍ ꜰʀᴇᴇ ᴛʀᴀɪʟ ʏᴇᴛ."
        else:
            updated_count = await db.reset_free_trial()
            message_text = f"ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ʀᴇꜱᴇᴛ ꜰʀᴇᴇ ᴛʀᴀɪʟ ꜰᴏʀ {updated_count} ᴜꜱᴇʀꜱ."
        await message.reply_text(message_text)
    except Exception as e:
        await message.reply_text(f"An error occurred: {e}")
