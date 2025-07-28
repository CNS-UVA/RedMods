import asyncio
import json
import logging
import os
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import aiohttp
import asyncpg
import discord
from aiohttp import web
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.utils import OneLogin_Saml2_Utils
from redbot.core import commands, Config, data_manager
from redbot.core.bot import Red

log = logging.getLogger("red.samlauth")


class SAMLAuth(commands.Cog):
    """SAML 2.0 authentication service provider."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2345678901)
        default_guild = {
            "enabled": False,
            "web_port": 6969,
            "base_url": "http://localhost:6969",
            "role_mappings": {},
            "role_dependencies": {}
        }
        self.config.register_guild(**default_guild)
        
        self.web_app = None
        self.web_runner = None
        self.web_site = None
        self.cleanup_task = None
        self.db_pool = None
        
        # Database configuration
        self.db_config = {
            'host': os.getenv('POSTGRES_HOST', 'postgres'),
            'port': int(os.getenv('POSTGRES_PORT', '5432')),
            'database': os.getenv('POSTGRES_DB', 'redbot'),
            'user': os.getenv('POSTGRES_USER', 'redbot'),
            'password': os.getenv('POSTGRES_PASSWORD', '')
        }
        
        asyncio.create_task(self._setup_database())
        # Start the cleanup task
        self.cleanup_task = asyncio.create_task(self._start_cleanup_task())

    async def _setup_database(self):
        """Initialize the PostgreSQL database for storing user authentication data."""
        try:
            # Create connection pool
            self.db_pool = await asyncpg.create_pool(**self.db_config)
            
            # Create table and indexes
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS saml_users (
                        id SERIAL PRIMARY KEY,
                        discord_user_id TEXT UNIQUE,
                        attributes JSONB,
                        verification_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        reminder_date TIMESTAMP,
                        expiration_date TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                await conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_discord_user_id ON saml_users(discord_user_id)
                ''')
                await conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_expiration_date ON saml_users(expiration_date)
                ''')
                await conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_reminder_date ON saml_users(reminder_date)
                ''')
                
                log.info("SAML database initialized successfully")
                
        except Exception as e:
            log.error(f"Failed to initialize SAML database: {e}")
            self.db_pool = None

    async def _init_saml_auth(self, req):
        """Initialize SAML auth object from aiohttp request."""
        # Handle reverse proxy headers
        forwarded_proto = req.headers.get('X-Forwarded-Proto', req.scheme)
        forwarded_host = req.headers.get('X-Forwarded-Host', req.headers.get('Host', ''))
        forwarded_port = req.headers.get('X-Forwarded-Port')
        
        # Force HTTPS and correct host for SAML
        is_https = forwarded_proto == 'https'
        
        if not forwarded_port:
            forwarded_port = '443' if is_https else '80'
        
        # Get base URL for dynamic settings
        base_url = f"{'https' if is_https else 'http'}://{forwarded_host}"
        if forwarded_port not in ['80', '443']:
            base_url += f":{forwarded_port}"
        
        # Get POST data if this is a POST request
        post_data = {}
        if req.method == 'POST':
            post_data = dict(await req.post())
        
        return OneLogin_Saml2_Auth({
            'https': 'on' if is_https else 'off',
            'http_host': forwarded_host,
            'server_port': int(forwarded_port),
            'script_name': req.path,
            'get_data': dict(req.query),
            'post_data': post_data
        }, self._get_saml_settings(base_url))

    def _get_saml_settings(self, base_url=None):
        """Load SAML settings from /app/saml.json with dynamic URLs."""
        settings_file = '/app/saml.json'
        
        settings = {}
        if os.path.exists(settings_file):
            with open(settings_file, 'r') as f:
                settings = json.load(f)
        else:
            log.warning(f"SAML settings file not found at {settings_file}")
            return {}
        
        # Update URLs with current base_url if provided
        if base_url and 'sp' in settings:
            settings['sp']['entityId'] = f"{base_url}/metadata/"
            settings['sp']['assertionConsumerService']['url'] = f"{base_url}/?acs"
            settings['sp']['singleLogoutService']['url'] = f"{base_url}/?sls"
        
        return settings

    async def _store_user_data(self, discord_user_id: str, attributes: Dict[str, Any]):
        """Store user authentication data in the database with expiration dates."""
        if not self.db_pool:
            log.error("Database pool not available")
            return
            
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO saml_users 
                    (discord_user_id, attributes, verification_date, reminder_date, expiration_date, updated_at)
                    VALUES ($1, $2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '365 days', CURRENT_TIMESTAMP + INTERVAL '395 days', CURRENT_TIMESTAMP)
                    ON CONFLICT (discord_user_id) DO UPDATE SET
                        attributes = EXCLUDED.attributes,
                        verification_date = CURRENT_TIMESTAMP,
                        reminder_date = CURRENT_TIMESTAMP + INTERVAL '365 days',
                        expiration_date = CURRENT_TIMESTAMP + INTERVAL '395 days',
                        updated_at = CURRENT_TIMESTAMP
                ''', discord_user_id, json.dumps(attributes))
        except Exception as e:
            log.error(f"Failed to store user data: {e}")

    async def _get_user_data(self, discord_user_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve user authentication data from the database."""
        if not self.db_pool:
            log.error("Database pool not available")
            return None
            
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchrow(
                    'SELECT attributes FROM saml_users WHERE discord_user_id = $1',
                    discord_user_id
                )
                
            if result:
                return {
                    'attributes': json.loads(result['attributes']) if isinstance(result['attributes'], str) else result['attributes']
                }
        except Exception as e:
            log.error(f"Failed to get user data: {e}")
            
        return None

    async def _handle_root(self, request):
        """Handle root path requests based on query parameters."""
        query_params = dict(request.query)
        
        if 'acs' in query_params:
            return await self._saml_acs(request)
        elif 'sls' in query_params:
            return await self._saml_sls(request)
        else:
            # Default behavior - show available endpoints
            return web.Response(text="""
SAML Service Provider Endpoints:
- GET /?acs - SAML Assertion Consumer Service
- GET/POST /?sls - SAML Single Logout Service  
- GET /metadata/ - SAML Metadata
- GET /login - Initiate SAML Login
            """, content_type='text/plain')

    async def _saml_login(self, request):
        """Handle SAML login initiation."""
        auth = await self._init_saml_auth(request)
        return web.Response(status=302, headers={'Location': auth.login()})

    async def _saml_acs(self, request):
        """Handle SAML Assertion Consumer Service (ACS) response."""
        auth = await self._init_saml_auth(request)
        auth.process_response()
        
        errors = auth.get_errors()
        if not errors:
            # Authentication successful
            attributes = auth.get_attributes()
            
            # Log the received attributes for debugging
            log.info(f"SAML Authentication successful. Attributes received: {list(attributes.keys())}")
            
            # For now, we'll need a way to link Discord users to SAML users
            # This could be done through a temporary token system or session
            session_id = request.cookies.get('discord_session')
            if session_id:
                # Store the SAML data temporarily with session ID
                # In a real implementation, you'd want a more secure approach
                pass
            
            # Store the attributes even without Discord user linking for now
            # This allows manual verification of what attributes are being received
            return web.Response(text=f"Authentication successful! Received attributes: {list(attributes.keys())}. You can close this window.")
        else:
            error_msg = f"SAML Authentication failed: {', '.join(errors)}"
            log.error(error_msg)
            return web.Response(text=error_msg, status=400)

    async def _saml_sls(self, request):
        """Handle SAML Single Logout Service (SLS)."""
        auth = await self._init_saml_auth(request)
        url = auth.process_slo(delete_session_cb=lambda: None)
        errors = auth.get_errors()
        
        if not errors:
            if url:
                return web.Response(status=302, headers={'Location': url})
            else:
                return web.Response(text="Logged out successfully!")
        else:
            error_msg = f"SLS error: {', '.join(errors)}"
            log.error(error_msg)
            return web.Response(text=error_msg, status=400)

    async def _saml_metadata(self, request):
        """Serve SAML metadata."""
        # Get base URL for dynamic settings
        url_data = urlparse(str(request.url))
        base_url = f"{request.scheme}://{request.host}"
        if url_data.port and url_data.port not in [80, 443]:
            base_url += f":{url_data.port}"
        
        settings = OneLogin_Saml2_Settings(self._get_saml_settings(base_url))
        metadata = settings.get_sp_metadata()
        errors = settings.check_sp_settings()
        
        if not errors:
            return web.Response(text=metadata, content_type='text/xml')
        else:
            error_msg = f"Metadata error: {', '.join(errors)}"
            log.error(error_msg)
            return web.Response(text=error_msg, status=500)

    async def start_web_server(self, guild_id: int):
        """Start the SAML web server."""
        guild_config = await self.config.guild_from_id(guild_id).all()
        
        if self.web_runner:
            await self.stop_web_server()
        
        self.web_app = web.Application()
        self.web_app.router.add_get('/', self._handle_root)
        self.web_app.router.add_get('/metadata/', self._saml_metadata)
        self.web_app.router.add_get('/login', self._saml_login)
        # Handle both GET and POST for backwards compatibility
        self.web_app.router.add_get('/', self._handle_root)
        self.web_app.router.add_post('/', self._handle_root)
        
        self.web_runner = web.AppRunner(self.web_app)
        await self.web_runner.setup()
        
        port = guild_config['web_port']
        self.web_site = web.TCPSite(self.web_runner, '0.0.0.0', port)
        await self.web_site.start()
        
        log.info(f"SAML web server started on port {port}")

    async def stop_web_server(self):
        """Stop the SAML web server."""
        if self.web_site:
            await self.web_site.stop()
            self.web_site = None
        
        if self.web_runner:
            await self.web_runner.cleanup()
            self.web_runner = None
        
        self.web_app = None
        log.info("SAML web server stopped")

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def samlauth(self, ctx):
        """Configure SAML authentication settings."""
        pass

    @samlauth.command()
    async def enable(self, ctx):
        """Enable SAML authentication for this server."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await self.start_web_server(ctx.guild.id)
        await ctx.send("SAML authentication enabled and web server started.")

    @samlauth.command()
    async def disable(self, ctx):
        """Disable SAML authentication for this server."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await self.stop_web_server()
        await ctx.send("SAML authentication disabled and web server stopped.")

    @samlauth.command()
    async def port(self, ctx, port: int):
        """Set the web server port."""
        await self.config.guild(ctx.guild).web_port.set(port)
        await ctx.send(f"Web server port set to {port}. Restart the server for changes to take effect.")

    @samlauth.command()
    async def baseurl(self, ctx, url: str):
        """Set the base URL for the SAML service provider."""
        await self.config.guild(ctx.guild).base_url.set(url)
        await ctx.send(f"Base URL set to {url}")

    @samlauth.command()
    async def status(self, ctx):
        """Show SAML authentication status."""
        guild_config = await self.config.guild(ctx.guild).all()
        
        embed = discord.Embed(title="SAML Authentication Status", color=0x3498db)
        embed.add_field(name="Enabled", value=guild_config["enabled"], inline=True)
        embed.add_field(name="Web Port", value=guild_config["web_port"], inline=True)
        embed.add_field(name="Base URL", value=guild_config["base_url"], inline=False)
        embed.add_field(name="Server Running", value=bool(self.web_runner), inline=True)
        
        await ctx.send(embed=embed)

    @samlauth.command()
    async def link(self, ctx, member: discord.Member, *oid_values: str):
        """Manually link a Discord user to their OID attribute values.
        
        This creates a database entry linking the Discord user to their SAML OID list.
        You can provide multiple values separated by spaces.
        
        Example: `[p]samlauth link @user student faculty`
        """
        if not oid_values:
            await ctx.send("Please provide at least one OID value.")
            return
        
        # Create attributes dict with the OID list
        attributes = {
            'urn:oid:1.3.6.1.4.1.5923.1.1.1.1': list(oid_values)
        }
        
        await self._store_user_data(str(member.id), attributes)
        await ctx.send(f"Linked {member.mention} to OID values: `{', '.join(oid_values)}`")

    @samlauth.command()
    async def unlink(self, ctx, member: discord.Member):
        """Remove the SAML link for a Discord user."""
        if not self.db_pool:
            await ctx.send("Database not available.")
            return
            
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.execute(
                    'DELETE FROM saml_users WHERE discord_user_id = $1',
                    str(member.id)
                )
                
            rows_affected = int(result.split()[1])  # Parse "DELETE <count>" response
            
            if rows_affected > 0:
                await ctx.send(f"Removed SAML link for {member.mention}")
            else:
                await ctx.send(f"No SAML link found for {member.mention}")
        except Exception as e:
            log.error(f"Failed to unlink user: {e}")
            await ctx.send("Failed to remove SAML link.")

    @samlauth.command()
    async def listusers(self, ctx):
        """List all users with SAML authentication data."""
        if not self.db_pool:
            await ctx.send("Database not available.")
            return
            
        try:
            async with self.db_pool.acquire() as conn:
                users = await conn.fetch('SELECT discord_user_id, attributes FROM saml_users')
            
            if not users:
                await ctx.send("No SAML users found in database.")
                return
            
            embed = discord.Embed(title="SAML Authenticated Users", color=0x3498db)
            
            for user in users:
                discord_id = user['discord_user_id']
                attributes_data = user['attributes']
                
                member = ctx.guild.get_member(int(discord_id))
                member_name = member.display_name if member else f"Unknown ({discord_id})"
                
                # Parse attributes to show key ones
                try:
                    attributes = attributes_data if isinstance(attributes_data, dict) else json.loads(attributes_data)
                    oid_list = attributes.get('urn:oid:1.3.6.1.4.1.5923.1.1.1.1', [])
                    oid_display = ', '.join(oid_list) if oid_list else 'None'
                    
                    embed.add_field(
                        name=member_name,
                        value=f"OID Values: `{oid_display}`\nTotal Attributes: {len(attributes)}",
                        inline=True
                    )
                except (json.JSONDecodeError, TypeError):
                    embed.add_field(
                        name=member_name,
                        value=f"Attributes: Invalid data",
                        inline=True
                    )
            
            await ctx.send(embed=embed)
        except Exception as e:
            log.error(f"Failed to list users: {e}")
            await ctx.send("Failed to retrieve user list.")

    @samlauth.command()
    async def cleanup(self, ctx):
        """Remove expired entries from the database."""
        if not self.db_pool:
            await ctx.send("Database not available.")
            return
            
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.execute('''
                    DELETE FROM saml_users 
                    WHERE expiration_date < CURRENT_TIMESTAMP
                ''')
                
            rows_deleted = int(result.split()[1])  # Parse "DELETE <count>" response
            await ctx.send(f"Cleaned up {rows_deleted} expired entries from the database.")
        except Exception as e:
            log.error(f"Failed to cleanup expired entries: {e}")
            await ctx.send("Failed to cleanup expired entries.")

    @samlauth.command()
    async def reminders(self, ctx):
        """Show users who need verification reminders."""
        if not self.db_pool:
            await ctx.send("Database not available.")
            return
            
        try:
            async with self.db_pool.acquire() as conn:
                reminder_users = await conn.fetch('''
                    SELECT discord_user_id, attributes, reminder_date, expiration_date
                    FROM saml_users 
                    WHERE reminder_date <= CURRENT_TIMESTAMP AND expiration_date > CURRENT_TIMESTAMP
                    ORDER BY expiration_date
                ''')
            
            if not reminder_users:
                await ctx.send("No users need verification reminders.")
                return
            
            embed = discord.Embed(title="Users Needing Verification Reminders", color=0xf39c12)
            
            for user in reminder_users:
                discord_id = user['discord_user_id']
                attributes_data = user['attributes']
                expiration_date = user['expiration_date']
                
                member = ctx.guild.get_member(int(discord_id))
                member_name = member.display_name if member else f"Unknown ({discord_id})"
                
                # Extract OID values from attributes
                try:
                    attributes = attributes_data if isinstance(attributes_data, dict) else json.loads(attributes_data)
                    oid_list = attributes.get('urn:oid:1.3.6.1.4.1.5923.1.1.1.1', [])
                    oid_display = ', '.join(oid_list) if oid_list else 'None'
                except (json.JSONDecodeError, TypeError):
                    oid_display = 'Invalid data'
                
                embed.add_field(
                    name=member_name,
                    value=f"OID Values: `{oid_display}`\nExpires: {expiration_date}",
                    inline=True
                )
            
            await ctx.send(embed=embed)
        except Exception as e:
            log.error(f"Failed to get reminder users: {e}")
            await ctx.send("Failed to retrieve reminder users.")

    async def _start_cleanup_task(self):
        """Start the periodic cleanup task."""
        while True:
            try:
                await asyncio.sleep(86400)  # Run daily
                
                if not self.db_pool:
                    continue
                    
                async with self.db_pool.acquire() as conn:
                    result = await conn.execute('''
                        DELETE FROM saml_users 
                        WHERE expiration_date < CURRENT_TIMESTAMP
                    ''')
                    
                rows_deleted = int(result.split()[1])  # Parse "DELETE <count>" response
                if rows_deleted > 0:
                    log.info(f"Automatically cleaned up {rows_deleted} expired SAML entries")
            except Exception as e:
                log.error(f"Error in cleanup task: {e}")

    def cog_unload(self):
        """Clean up when the cog is unloaded."""
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
        if self.web_runner:
            asyncio.create_task(self.stop_web_server())
        if self.db_pool:
            asyncio.create_task(self.db_pool.close())