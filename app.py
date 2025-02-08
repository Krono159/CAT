import aiosqlite
import asyncio
import audioop
import av
import discord
import hashlib
import io
import json
import logging
import os
import platform
import random
import requests
import signal
import sys
import time
import threading
import wave

from array import array
from discord import PCMVolumeTransformer, AudioSource
from discord.opus import Encoder as OpusEncoder
from discord.ui import View, Button
from discord import ButtonStyle
import requests
from datetime import date, datetime
from discord.ext import commands, tasks
from discord.ext.commands import Context
from dotenv import load_dotenv
from database import DatabaseManager
from cogs.helper.queuehelper import QueueHelper
from cogs.models.track_playlist import Track, TrackPlaylist
import asyncio
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp import YoutubeDL


listener_ready = False  # Variable global para indicar que el listener está listo

if not os.path.isfile(f"{os.path.realpath(os.path.dirname(__file__))}/config.json"):
    sys.exit("'config.json' not found! Please add it and try again.")
else:
    with open(f"{os.path.realpath(os.path.dirname(__file__))}/config.json", encoding='utf-8') as file:
        config = json.load(file)

if 'colors' not in config or 'default' not in config['colors']:
    sys.exit("'colors' or 'colors.default' not found in config.json! Please add it and try again. Defaulting...")

"""
Setup bot intents (events restrictions)
For more information about intents, please go to the following websites:
https://discordpy.readthedocs.io/en/latest/intents.html
https://discordpy.readthedocs.io/en/latest/intents.html#privileged-intents
"""

intents = discord.Intents.default()
intents.bans = True
intents.dm_messages = True
intents.dm_reactions = True
intents.dm_typing = True
intents.emojis = True
intents.messages = True
intents.members = True
intents.emojis_and_stickers = True
intents.guild_messages = True

boottime = date.today()
print(boottime)


# Setup both of the loggers

class MP3AudioSource(AudioSource):
    def __init__(self, filename):
        import pydub
        # Initialize volume control much lower
        self._volume = 0.5  # Changed from 0.8
        
        # Load MP3 with original settings
        self.audio = pydub.AudioSegment.from_mp3(filename)
        
        # Convert to Discord compatible format
        self.audio = self.audio.set_channels(2)
        self.audio = self.audio.set_frame_rate(48000)
        self.audio = self.audio.set_sample_width(2)
        
        # Apply effects
        self.audio = self.audio.fade_in(20).fade_out(50)
        self.audio = self.audio + pydub.AudioSegment.silent(duration=50)
        
        # Export to WAV buffer
        self._buffer = io.BytesIO()
        self.audio.export(
            self._buffer,
            format='wav',
            parameters=["-ac", "2", "-ar", "48000", "-sample_fmt", "s16"]
        )
        self._buffer.seek(0)
        self._wave = wave.open(self._buffer, 'rb')
        self._end = False

    @property
    def volume(self):
        return self._volume

    @volume.setter 
    def volume(self, value):
        # Clamp volume between 0.0 and 0.5
        self._volume = max(0.0, min(0.5, value))

    def read(self) -> bytes:
        if self._end:
            return b''
        try:
            data = self._wave.readframes(960)
            if not data or len(data) < 3840:
                self._end = True
                return b''
                
            # Always apply volume transformation
            data_array = array('h', data)
            for i in range(len(data_array)):
                data_array[i] = int(data_array[i] * self._volume)
            return data_array.tobytes()
            
        except Exception as e:
            print(f"Read error: {e}")
            self._end = True
            return b''
        
    def cleanup(self):
        self._wave.close()
        self._buffer.close()

    def is_opus(self) -> bool:
        return False
    
class LoggingFormatter(logging.Formatter):
    # Colors
    black = "\x1b[30m"
    red = "\x1b[31m"
    green = "\x1b[32m"
    yellow = "\x1b[33m"
    blue = "\x1b[34m"
    gray = "\x1b[38m"
    # Styles
    reset = "\x1b[0m"
    bold = "\x1b[1m"

    COLORS = {
        logging.DEBUG: gray + bold,
        logging.INFO: blue + bold,
        logging.WARNING: yellow + bold,
        logging.ERROR: red,
        logging.CRITICAL: red + bold,
    }

    def format(self, record):
        log_color = self.COLORS[record.levelno]
        format = "{asctime} {levelcolor}{levelname:<8}{reset} {green}{name}{reset} {message}"
        format = format.replace("{levelcolor}", log_color)
        format = format.replace("{green}", self.green + self.bold)
        format = format.replace("{reset}", self.reset)
        formatter = logging.Formatter(format, "%Y-%m-%d %H:%M:%S", style="{")
        return formatter.format(record)

class CustomAudioSource(AudioSource):
    def __init__(self, url):
        self.url = url
        self.stream = None
        # Get direct stream URL using yt-dlp
        ytdl = YoutubeDL({
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True
        })
        info = ytdl.extract_info(url, download=False)
        self.stream_url = info.get('url', url)
        self._init_stream()

    def _init_stream(self):
        try:
            response = requests.get(self.stream_url, stream=True)
            container = av.open(io.BytesIO(response.content))
            self.stream = container.decode(audio=0)
        except Exception as e:
            print(f"Stream init error: {e}")

    def read(self) -> bytes:
        if not self.stream:
            self._init_stream()
            if not self.stream:
                return b''
        try:
            frame = next(self.stream)
            return frame.planes[0].to_bytes()
        except StopIteration:
            return b''
        except Exception:
            self._init_stream()
            return b''

    def cleanup(self):
        if self.stream:
            self.stream = None

class AudioSource:
    def __init__(self, url):
        self.url = url
        self.stream = None
        
    def read(self):
        if not self.stream:
            response = requests.get(self.url, stream=True)
            container = av.open(io.BytesIO(response.content))
            self.stream = container.decode(audio=0)
            
        try:
            frame = next(self.stream)
            return frame.planes[0].to_bytes()
        except StopIteration:
            return b''


logger = logging.getLogger('KuroLogger:\t')
logger.setLevel(logging.DEBUG)  # Set to DEBUG to capture all levels of logs

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(LoggingFormatter())

# File handler
file_handler = logging.FileHandler(filename="./logs/" + str(boottime) + "-discord.log", encoding="utf-8")
file_handler_formatter = logging.Formatter(
    "[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{"
)
file_handler.setFormatter(file_handler_formatter)

# Add the handlers
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Set up logging for discord library
discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.DEBUG)  # Set to DEBUG to capture all levels of logs

# Elimina todos los manejadores del logger de Discord
for handler in discord_logger.handlers[:]:
    discord_logger.removeHandler(handler)

# Añade los manejadores personalizados al logger de Discord
discord_logger.addHandler(console_handler)
discord_logger.addHandler(file_handler)

print(config["prefix"])

class BufferedAudioSource(AudioSource):
    def __init__(self, url, buffer_size=5000000):  # 5MB buffer
        self.url = url
        self.buffer = io.BytesIO()
        self.read_thread = None
        self.download_thread = None
        self.buffer_size = buffer_size
        self.buffer_lock = threading.Lock()
        self.stopping = False
        self.download_complete = False
        self._start_download()

    def _start_download(self):
        def download():
            try:
                response = requests.get(self.url, stream=True)
                for chunk in response.iter_content(chunk_size=8192):
                    if self.stopping:
                        break
                    with self.buffer_lock:
                        self.buffer.write(chunk)
                        if self.buffer.tell() > self.buffer_size:
                            # Trim buffer if too large
                            data = self.buffer.getvalue()
                            self.buffer = io.BytesIO(data[-self.buffer_size:])
                self.download_complete = True
            except Exception as e:
                print(f"Download error: {e}")

        self.download_thread = threading.Thread(target=download)
        self.download_thread.start()

    def read(self) -> bytes:
        with self.buffer_lock:
            if self.buffer.tell() < 3840:  # Discord voice packet size
                if self.download_complete:
                    return b''
                return b'\x00' * 3840  # Send silence while buffering
                
            self.buffer.seek(0)
            data = self.buffer.read(3840)
            remaining = self.buffer.read()
            self.buffer.seek(0)
            self.buffer.write(remaining)
            self.buffer.truncate()
            return data

    def cleanup(self):
        self.stopping = True
        if self.download_thread and self.download_thread.is_alive():
            self.download_thread.join()
        self.buffer.close()
        
class DiscordBot(commands.Bot):
    
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or("cat!"),
            intents=intents,
            help_command=None,
        )
        self.logger = logger
        self.config = config
        self.database = None
        self.music = MusicManager(self)
        self.helpers = QueueHelper()
        self.spotify = SpotifyAPI(
            client_id=self.config["spotify"]["client_id"],
            client_secret=self.config["spotify"]["client_secret"]
        )
    

    async def init_db(self) -> None:
        async with aiosqlite.connect(
                f"{os.path.realpath(os.path.dirname(__file__))}/database/database.db"
        ) as db:
            with open(
                    f"{os.path.realpath(os.path.dirname(__file__))}/database/schema.sql"
            ) as file:
                await db.executescript(file.read())
            await db.commit()

    async def setup_hook(self) -> None:
        print("Syncing commands...")
        for guild in self.guilds:
            try:
                await self.tree.sync(guild=guild)
                print(f"Synced commands for guild: {guild.name}")
            except Exception as e:
                print(f"Failed to sync commands for guild {guild.name}: {e}")
        print("Command sync completed!")

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        try:
            await self.tree.sync(guild=guild)
            print(f"Synced commands for new guild: {guild.name}")
        except Exception as e:
            print(f"Failed to sync commands for new guild {guild.name}: {e}")

    async def on_member_join(self, member):
        if member.bot:
            try:
                guild_id = str(member.guild.id)

                # Load current config
                with open(f"{os.path.realpath(os.path.dirname(__file__))}/config.json", "r") as config_file:
                    current_config = json.load(config_file)

                # Check if guild has configured bot role
                if "guild_bot_roles" not in current_config:
                    current_config["guild_bot_roles"] = {}

                if guild_id not in current_config["guild_bot_roles"]:
                    # Create new role
                    bot_role = await member.guild.create_role(name="Bots", reason="Automatic role for bots")
                    current_config["guild_bot_roles"][guild_id] = bot_role.id

                    # Save updated config
                    with open(f"{os.path.realpath(os.path.dirname(__file__))}/config.json", "w") as configjson:
                        json.dump(current_config, configjson, indent=2)

                # Get role and assign it
                role_id = current_config["guild_bot_roles"][guild_id]
                bot_role = member.guild.get_role(int(role_id))
                if (bot_role):
                    await member.add_roles(bot_role)
                    print(f"Added bot role to {member.name} in guild {member.guild.name}")

            except Exception as e:
                print(f"Error managing bot role: {e}")

    async def load_cogs(self) -> None:
        if listener_ready:
            self.logger.info(f'==============================================')
            self.logger.info(f'--BOT BOOTED AT {str(datetime.now())}--')
            self.logger.info(f'==============================================')
            self.logger.info(f'-CONSOLE ENABLED AT {str(datetime.now())}-')
            self.logger.info(f'==============================================')

        else:
            self.logger.info(f'==============================================')
            self.logger.info(f'--BOT BOOTED AT {str(datetime.now())}--')
            self.logger.info(f'==============================================')

        for cog_file in os.listdir(f"{os.path.realpath(os.path.dirname(__file__))}/cogs"):
            if cog_file.endswith(".py"):
                extension = cog_file[:-3]
                try:
                    await self.load_extension(f"cogs.{extension}")
                    self.logger.info(f"Loaded extension '{extension}'")
                except Exception as e:
                    exception = f"{type(e).__name__}: {e}"
                    self.logger.error(
                        f"Failed to load extension {extension}\n{exception}"
                    )

    @tasks.loop(minutes=0.2)
    async def status_task(self) -> None:
        statuses = ["to be a bot", "with the API", "with the code", "with the database","with the logs","with The Devs",
                    "with catnip", "to drink w/TanuBeer"]
        await self.change_presence(activity=discord.Game(random.choice(statuses)))

    @status_task.before_loop
    async def before_status_task(self) -> None:
        """
        Before starting the status changing task, we make sure the bot is ready
        """
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        """
        This will just be executed when the bot starts the first time.
        """
        self.logger.info(f"Logged in as {self.user.name}")
        self.logger.info(f"discord.py API version: {discord.__version__}")
        self.logger.info(f"Python version: {platform.python_version()}")
        self.logger.info(
            f"Running on: {platform.system()} {platform.release()} ({os.name})"
        )
        self.logger.info("-------------------")
        await self.init_db()
        await self.load_cogs()
        self.status_task.start()
        self.database = DatabaseManager(
            connection=await aiosqlite.connect(
                f"{os.path.realpath(os.path.dirname(__file__))}/database/database.db"
            )
        )

    async def on_message(self, message: discord.Message) -> None:
        """
        The code in this event is executed every time someone sends a message, with or without the prefix

        :param message: The message that was sent.
        """
        if message.author == self.user or message.author.bot:
            return

        # Convert to lowercase if starts with mention
        if message.content.startswith(f'<@{self.user.id}>'):
            message.content = message.content.lower()
            # Remove double spaces after mention
            while f'<@{self.user.id}>  ' in message.content:
                message.content = message.content.replace(f'<@{self.user.id}>  ', f'<@{self.user.id}> ')

        await self.process_commands(message)
    async def on_command_completion(self, context: Context) -> None:
        """
        The code in this event is executed every time a normal command has been *successfully* executed.

        :param context: The context of the command that has been executed.
        """
        full_command_name = context.command.qualified_name
        split = full_command_name.split(" ")
        executed_command = str(split[0])
        if context.guild is not None:
            self.logger.info(
                f"Executed {executed_command} command in {context.guild.name} (ID: {context.guild.id}) by {context.author} (ID: {context.author.id})"
            )
        else:
            self.logger.info(
                f"Executed {executed_command} command by {context.author} (ID: {context.author.id}) in DMs"
            )

    async def on_command_error(self, context: Context, error) -> None:
        #Error command for (usually) text commands. this will stay active to keep log of errors and delete error embeds after a certain time.

        if isinstance(error, commands.CommandOnCooldown):
            minutes, seconds = divmod(error.retry_after, 60)
            hours, minutes = divmod(minutes, 60)
            hours = hours % 24
            embed = discord.Embed(
                description=f"**Please slow down** - You can use this command again in {f'{round(hours)} hours' if round(hours) > 0 else ''} {f'{round(minutes)} minutes' if round(minutes) > 0 else ''} {f'{round(seconds)} seconds' if round(seconds) > 0 else ''}.",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
            time.sleep(15)
            try:
                await context.channel.purge(limit=2)

            #Exception block. bot will try to delete the last message 3 times

            except Exception as e:
                o = str(e)
                print(f'error: Failed to delete, error:\n{o}. trying again')
                try:
                    await context.channel.purge(limit=2)
                except Exception as e:
                    o = str(e)
                    print(f'error: Failed to delete, error:\n{o}. trying again')
                    try:
                        await context.channel.purge(limit=2)
                    except Exception as e:
                        o = str(e)
                        print(f'error: Failed to delete, error:\n{o}. trying again')
                        try:
                            await context.channel.purge(limit=2)
                        except Exception as e:
                            o = str(e)
                            print(f'error: Failed to delete, error:\n{o}. cannot delete message after 3 attempts. check the logs, if required, reboot')

        elif isinstance(error, commands.NotOwner):
            embed = discord.Embed(
                description="You are not the owner of the bot!", color=0xE02B2B
            )
            await context.send(embed=embed)
            time.sleep(15)
            try:
                await context.channel.purge(limit=2)

            #Exception block. bot will try to delete the last message 3 times

            except Exception as e:
                o = str(e)
                print(f'error: Failed to delete, error:\n{o}. trying again')
                try:
                    await context.channel.purge(limit=2)
                except Exception as e:
                    o = str(e)
                    print(f'error: Failed to delete, error:\n{o}. trying again')
                    try:
                        await context.channel.purge(limit=2)
                    except Exception as e:
                        o = str(e)
                        print(f'error: Failed to delete, error:\n{o}. trying again')
                        try:
                            await context.channel.purge(limit=2)
                        except Exception as e:
                            o = str(e)
                            print(f'error: Failed to delete, error:\n{o}. cannot delete message after 3 attempts. check the logs, if required, reboot')

            if context.guild:
                self.logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner only command in the guild {context.guild.name} (ID: {context.guild.id}), but the user is not an owner of the bot."
                )
            else:
                self.logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner only command in the bot's DMs, but the user is not an owner of the bot."
                )

        elif isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                description="You are missing the permission(s) `"
                            + ", ".join(error.missing_permissions)
                            + "` to execute this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
            time.sleep(15)
            try:
                await context.channel.purge(limit=2)

            #Exception block. bot will try to delete the last message 3 times

            except Exception as e:
                o = str(e)
                print(f'error: Failed to delete, error:\n{o}. trying again')
                try:
                    await context.channel.purge(limit=2)
                except Exception as e:
                    o = str(e)
                    print(f'error: Failed to delete, error:\n{o}. trying again')
                    try:
                        await context.channel.purge(limit=2)
                    except Exception as e:
                        o = str(e)
                        print(f'error: Failed to delete, error:\n{o}. trying again')
                        try:
                            await context.channel.purge(limit=2)
                        except Exception as e:
                            o = str(e)
                            print(f'error: Failed to delete, error:\n{o}. cannot delete message after 3 attempts. check the logs, if required, reboot')

        elif isinstance(error, commands.BotMissingPermissions):
            embed = discord.Embed(
                description="I am missing the permission(s) `"
                            + ", ".join(error.missing_permissions)
                            + "` to fully perform this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
            time.sleep(15)
            try:
                await context.channel.purge(limit=2)
            
            #Exception block. bot will try to delete the last message 3 times
            
            except Exception as e:
                o = str(e)
                print(f'error: Failed to delete, error:\n{o}. trying again')
                try:
                    await context.channel.purge(limit=2)
                except Exception as e:
                    o = str(e)
                    print(f'error: Failed to delete, error:\n{o}. trying again')
                    try:
                        await context.channel.purge(limit=2)
                    except Exception as e:
                        o = str(e)
                        print(f'error: Failed to delete, error:\n{o}. trying again')
                        try:
                            await context.channel.purge(limit=2)
                        except Exception as e:
                            o = str(e)
                            print(f'error: Failed to delete, error:\n{o}. cannot delete message after 3 attempts. check the logs, if required, reboot')

        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="Error!",
                # We need to capitalize because the command arguments have no capital letter in the code, and they are the first word in the error message.
                description=str(error).capitalize(),
                color=0xE02B2B,
            )
            await context.send(embed=embed)
            time.sleep(15)
            try:
                await context.channel.purge(limit=2)
            
            #Exception block. bot will try to delete the last message 3 times
            
            except Exception as e:
                o = str(e)
                print(f'error: Failed to delete, error:\n{o}. trying again')
                try:
                    await context.channel.purge(limit=2)
                except Exception as e:
                    o = str(e)
                    print(f'error: Failed to delete, error:\n{o}. trying again')
                    try:
                        await context.channel.purge(limit=2)
                    except Exception as e:
                        o = str(e)
                        print(f'error: Failed to delete, error:\n{o}. trying again')
                        try:
                            await context.channel.purge(limit=2)
                        except Exception as e:
                            o = str(e)
                            print(f'error: Failed to delete, error:\n{o}. cannot delete message after 3 attempts. check the logs, if required, reboot')

        elif isinstance(error, commands.UserNotFound):
            embed = discord.Embed(
                    description=f"user not found.",
                    color=0xFF0000,
                )
            await context.send(embed=embed,ephemeral=True)
            time.sleep(15)
            try:
                await context.channel.purge(limit=2)
            
            #Exception block. bot will try to delete the last message 3 times

            except Exception as e:
                o = str(e)
                print(f'error: Failed to delete, error:\n{o}. trying again')
                try:
                    await context.channel.purge(limit=2)
                except Exception as e:
                    o = str(e)
                    print(f'error: Failed to delete, error:\n{o}. trying again')
                    try:
                        await context.channel.purge(limit=2)
                    except Exception as e:
                        o = str(e)
                        print(f'error: Failed to delete, error:\n{o}. trying again')
                        try:
                            await context.channel.purge(limit=2)
                        except Exception as e:
                            o = str(e)
                            print(f'error: Failed to delete, error:\n{o}. cannot delete message after 3 attempts. check the logs, if required, reboot')

        elif isinstance(error, commands.UserInputError):
            embed = discord.Embed(
                    description=f"there is an error on the command, check the syntax",
                    color=0xFF0000,
                )
            await context.send(embed=embed)
            time.sleep(15)
            time.sleep(15)
            try:
                await context.channel.purge(limit=2)
            
            #Exception block. bot will try to delete the last message 3 times

            except Exception as e:
                o = str(e)
                print(f'error: Failed to delete, error:\n{o}. trying again')
                try:
                    await context.channel.purge(limit=2)
                except Exception as e:
                    o = str(e)
                    print(f'error: Failed to delete, error:\n{o}. trying again')
                    try:
                        await context.channel.purge(limit=2)
                    except Exception as e:
                        o = str(e)
                        print(f'error: Failed to delete, error:\n{o}. trying again')
                        try:
                            await context.channel.purge(limit=2)
                        except Exception as e:
                            o = str(e)
                            print(f'error: Failed to delete, error:\n{o}. cannot delete message after 3 attempts. check the logs, if required, reboot')
           
        else:
            raise error

class SpotifyAPI:
    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise ValueError("Missing Spotify credentials")
            
        self.spotify = Spotify(
            client_credentials_manager=SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret
            )
        )
        # Updated YoutubeDL config
        self.ytdl = YoutubeDL({
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'default_search': 'ytsearch',
            'cookiefile': 'cookies.txt'
        })

    async def get_track_info(self, url: str) -> dict:
        try:
            track_id = url.split('/')[-1].split('?')[0]
            track = self.spotify.track(track_id)
            
            search_query = f"{track['name']} {track['artists'][0]['name']} audio"
            
            # Try direct YouTube search
            try:
                result = self.ytdl.extract_info(f"ytsearch:{search_query}", download=False)
                if result and 'entries' in result and result['entries']:
                    video = result['entries'][0]
                    return {
                        'title': track['name'],
                        'artist': track['artists'][0]['name'],
                        'url': video.get('url') or video.get('webpage_url'),
                        'duration': video.get('duration', 0),
                        'preview_url': track.get('preview_url')  # Add preview URL as fallback
                    }
            except Exception as yt_error:
                self.bot.logger.error(f"YouTube extraction failed: {yt_error}")
                
                # Fall back to Spotify preview URL if available
                preview_url = track.get('preview_url')
                if preview_url:
                    return {
                        'title': track['name'],
                        'artist': track['artists'][0]['name'],
                        'url': preview_url,  # Use preview URL as main URL
                        'duration': track['duration_ms'] // 1000,
                        'preview_url': preview_url
                    }
                    
            raise Exception("Could not get audio source from YouTube or Spotify")
            
        except Exception as e:
            raise Exception(f"Failed to get track info: {str(e)}")

    async def search_track(self, query: str) -> dict:
        """Search for a track on Spotify"""
        try:
            # Search Spotify
            result = self.spotify.search(query, type='track', limit=1)
            
            if not result['tracks']['items']:
                return None
                
            track = result['tracks']['items'][0]
            
            # Get YouTube URL for playback
            search_query = f"{track['name']} {track['artists'][0]['name']} audio"
            yt_result = self.ytdl.extract_info(f"ytsearch:{search_query}", download=False)
            
            if not yt_result or 'entries' not in yt_result or not yt_result['entries']:
                raise Exception("Could not find YouTube source")
                
            video = yt_result['entries'][0]
            
            return {
                'title': track['name'],
                'artist': track['artists'][0]['name'],
                'url': video.get('url') or video.get('webpage_url'),
                'duration': video.get('duration', 0)
            }
            
        except Exception as e:
            self.bot.logger.error(f"Search error: {str(e)}")
            return None 

class AudioFileReader:
    def __init__(self, file_path):
        self.file_path = file_path
        self.wave_file = wave.open(file_path, 'rb')
        self._end = False

    def read(self) -> bytes:
        if self._end:
            return b''
        try:
            data = self.wave_file.readframes(3840)  # Discord packet size
            if not data:
                self._end = True
                return b''
            return data
        except Exception as e:
            print(f"Read error: {e}")
            self._end = True
            return b''

    def cleanup(self):
        if hasattr(self, 'wave_file') and self.wave_file:
            self.wave_file.close()

    def is_opus(self) -> bool:
        return False

    def __del__(self):
        self.cleanup()

class Track:
    def __init__(self, title, url, duration, requester=None):
        self.title = title
        self.url = url
        self.duration = duration
        self.requester = requester
        self.stream_url = None  # Add stream URL field

class AudioReader(AudioSource):
    def __init__(self, file_path):
        self.file_path = file_path
        self.wave_file = wave.open(file_path, 'rb')
        self._end = False

    def read(self) -> bytes:
        if self._end:
            return b''
        try:
            data = self.wave_file.readframes(3840)  # Discord packet size
            if not data:
                self._end = True
                return b''
            return data
        except Exception as e:
            print(f"Read error: {e}")
            self._end = True
            return b''

    def cleanup(self):
        if hasattr(self, 'wave_file') and self.wave_file:
            self.wave_file.close()

    def is_opus(self) -> bool:
        return False
            
    def __del__(self):
        self.cleanup()

class AudioStreamReader(AudioSource):
    def __init__(self, file_path):
        self.file_path = file_path
        self.wave_file = wave.open(file_path, 'rb')
        self._end = False
        self._volume = 1.0

    def read(self) -> bytes:
        if self._end:
            return b''
        try:
            data = self.wave_file.readframes(3840)
            if not data:
                self._end = True
                return b''
            return data
        except Exception as e:
            print(f"Read error: {e}")
            self._end = True
            return b''

    def cleanup(self):
        if hasattr(self, 'wave_file') and self.wave_file:
            self.wave_file.close()

    def is_opus(self) -> bool:
        return False
            
    def __del__(self):
        self.cleanup()

class MusicPlayer:
    def __init__(self, bot, guild, voice_channel, text_channel):
        self.bot = bot
        self.guild = guild
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.queue = []
        self.current = None
        self.voice_client = None
        self.playing = False
        self.paused = False
        self.volume = 1.0  # Default volume (1.0 = 100%)
        self.download_queue = asyncio.Queue()
        self.downloading = False
        self.downloads_dir = os.path.join('downloads', str(guild.id))
        self.now_playing_message = None
        self.download_task = self.bot.loop.create_task(self.downloader())
        self._play_lock = asyncio.Lock()
        os.makedirs(self.downloads_dir, exist_ok=True)

    async def set_volume(self, volume: float):
        """Set volume for both player and current audio source"""
        self.volume = volume
        if self.voice_client and self.voice_client.source:
            self.voice_client.source.volume = volume

    def cleanup_old_files(self, current_file=None):
        try:
            files = [f for f in os.listdir(self.downloads_dir) if f.endswith('.mp3')]
            # Remove current file from list if exists
            if current_file and os.path.basename(current_file) in files:
                files.remove(os.path.basename(current_file))
            
            # Sort files by modification time
            files.sort(key=lambda x: os.path.getmtime(os.path.join(self.downloads_dir, x)))
            
            # Delete old files if more than 2
            while len(files) > 1:  # Keep only 1 old file + current playing
                old_file = os.path.join(self.downloads_dir, files.pop(0))
                try:
                    os.remove(old_file)
                except:
                    pass

        except Exception as e:
            self.bot.logger.error(f"Error cleaning old files: {e}")
            
    def get_cached_file(self, url: str) -> str:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return os.path.join(self.downloads_dir, f"{url_hash}.mp3")

    async def downloader(self):
        while True:
            try:
                url = await self.download_queue.get()
                self.downloading = True

                base_path = self.get_cached_file(url)
                cached_file = f"{base_path}.mp3"

                if not os.path.exists(cached_file):
                    self.bot.logger.info(f"Pre-downloading next song...")
                    with YoutubeDL({
                        'format': 'bestaudio',
                        'outtmpl': base_path,
                        'quiet': True,
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }],
                    }) as ydl:
                        await self.bot.loop.run_in_executor(None, ydl.download, [url])

                self.downloading = False
                self.download_queue.task_done()

            except Exception as e:
                self.bot.logger.error(f"Download error: {str(e)}")
                self.downloading = False
                self.download_queue.task_done()

    async def connect(self, ctx=None):
        try:
            if ctx and not ctx.author.voice:
                raise ValueError("You must be in a voice channel to use this command")

            target_channel = ctx.author.voice.channel if ctx else self.voice_channel
            if not target_channel:
                raise ValueError("Could not find voice channel")

            self.voice_channel = target_channel

            if self.voice_client and self.voice_client.is_connected():
                await self.voice_client.move_to(self.voice_channel)
            else:
                self.voice_client = await self.voice_channel.connect()
            return self.voice_client

        except Exception as e:
            self.bot.logger.error(f"Connection error: {e}")
            await self.cleanup()
            raise

    async def play(self):
        if not self.queue:
            self.playing = False
            return

        try:
            if not self.voice_client or not self.voice_client.is_connected():
                await self.connect()

            self.current = self.queue.pop(0)
            url = getattr(self.current, 'url', None) or getattr(self.current.first_track, 'url', None)
            
            if not url:
                raise ValueError("No valid URL found")

            base_path = self.get_cached_file(url)
            cached_file = self.get_cached_file(url)
            self.cleanup_old_files(cached_file)
            
            if not os.path.exists(cached_file):
                self.bot.logger.info("Downloading current track...")
                with YoutubeDL({
                    'format': 'bestaudio',
                    'outtmpl': cached_file[:-4],  # Remove .mp3 extension
                    'quiet': True,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                }) as ydl:
                    await self.bot.loop.run_in_executor(None, ydl.download, [url])


            if self.queue:
                next_track = self.queue[0]
                next_url = getattr(next_track, 'url', None) or getattr(next_track.first_track, 'url', None)
                if next_url:
                    await self.download_queue.put(next_url)

            self.bot.logger.info(f"Playing from: {cached_file}")
            source = MP3AudioSource(cached_file)
            
            if self.now_playing_message:
                try:
                    await self.now_playing_message.delete()
                except:
                    pass

            self.now_playing_message = await self.text_channel.send(
                f"🎵 Now playing: **{self.current.title}**"
            )
            self.playing = True

            self.voice_client.play(
                source,
                after=lambda e: asyncio.run_coroutine_threadsafe(
                    self.play_next(), self.bot.loop
                )
            )

        except Exception as e:
            self.bot.logger.error(f"Playback error: {str(e)}")
            await self.cleanup()
            await self.play_next()

    async def play_next(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        await self.play()

    async def skip(self):
        if not self.voice_client or not self.voice_client.is_playing():
            return None
            
        title = None
        if self.current:
            title = self.current.title if hasattr(self.current, 'title') else 'Unknown'
            
        self.voice_client.stop()
        return title

    async def pause(self, pause: bool = True):
        if not self.voice_client:
            return False

        if pause and not self.paused:
            self.voice_client.pause()
            self.paused = True
            return True
        elif not pause and self.paused:
            self.voice_client.resume()
            self.paused = False
            return True
        return False

    async def resume(self):
        return await self.pause(False)

    def is_playing(self):
        return (
            self.voice_client 
            and self.voice_client.is_connected()
            and self.voice_client.is_playing()
            and self.playing
            and not self.paused
        )

    def is_paused(self):
        return self.paused
    async def destroy(self):
        """Clean up resources and disconnect"""
        try:
            # Stop playing and clear queue
            if self.voice_client:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                await self.voice_client.disconnect()
                self.voice_client = None

            # Clear queue and current track
            self.queue.clear()
            self.current = None

            # Remove from bot's player list
            if self.guild.id in self.bot.music.players:
                del self.bot.music.players[self.guild.id]

        except Exception as e:
            print(f"Error destroying player: {e}")
            
    async def cleanup(self):
        if hasattr(self, 'download_task'):
            self.download_task.cancel()
        if self.voice_client:
            try:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                if self.voice_client.is_connected():
                    await self.voice_client.disconnect()
            except:
                pass
        self.voice_client = None
        self.playing = False
        self.paused = False
        if self.now_playing_message:
            try:
                await self.now_playing_message.delete()
            except:
                pass
            self.now_playing_message = None
    
    
class MusicManager:
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    def get_player(self, guild):
        """Get existing player for guild or None"""
        return self.players.get(guild.id)

    def create_player(self, guild, voice_channel, text_channel):
        """Create new player for guild"""
        player = MusicPlayer(self.bot, guild, voice_channel, text_channel)
        self.players[guild.id] = player
        return player

    async def cleanup(self, guild_id):
        if hasattr(self, 'download_task'):
            self.download_task.cancel()
        """Cleanup player for guild"""
        if guild_id in self.players:
            player = self.players[guild_id]
            await player.cleanup()
            del self.players[guild_id]

    def register_player(self, guild_id, player):
        """Register existing player"""
        self.players[guild_id] = player

    def remove_player(self, guild_id):
        """Remove player without cleanup"""
        if guild_id in self.players:
            del self.players[guild_id]

load_dotenv()
bot = DiscordBot()


def console_listener(bot):
    global listener_ready
    listener_ready = True  # Establecer la variable en True cuando el listener esté listo
    bot.logger.info("Listener is ready...")
    try:
        while True:
            command = input()
            if command.lower() == "stop":
                bot.logger.info("Stopping the bot...")
                try:
                    def force_stop():
                        bot.logger.info("Forcing stop...")
                        os.kill(os.getpid(), signal.SIGTERM)

                    force_stop()
                except Exception as e:
                    bot.logger.error(f"An error occurred while stopping the bot: {e}, forcing stop...")
                    os.kill(os.getpid(), signal.SIGTERM)
                break
            elif command.lower() == "restart":
                bot.logger.info("Restarting the bot...")
                try:
                    def restart_bot():
                        bot.logger.info("Restarting...")
                        os.execv(sys.executable, ['python'] + sys.argv)

                    restart_bot()
                except Exception as e:
                    bot.logger.error(f"An error occurred while restarting the bot: {e}, forcing restart...")
                    os.execv(sys.executable, ['python'] + sys.argv)
                break
    except Exception as e:
        bot.logger.error(f"An error occurred in the console listener: {e}")


# Inicia el listener de consola en un hilo separado
thread = threading.Thread(target=console_listener, args=(bot,))
thread.start()
bot.run(os.getenv("TOKEN"))
