import discord
from redbot.core import commands, Config
from redbot.core.bot import Red


class JoinMessage(commands.Cog):
    """Send welcome messages with links when users join."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        default_guild = {
            "enabled": False,
            "channel": None,
            "message": "Welcome {user}! Please visit {link} to get started.",
            "link": "https://example.com"
        }
        self.config.register_guild(**default_guild)

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def joinmessage(self, ctx):
        """Configure join message settings."""
        pass

    @joinmessage.command()
    async def enable(self, ctx):
        """Enable join messages for this server."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("Join messages enabled.")

    @joinmessage.command()
    async def disable(self, ctx):
        """Disable join messages for this server."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("Join messages disabled.")

    @joinmessage.command()
    async def channel(self, ctx, channel: discord.TextChannel):
        """Set the channel for join messages."""
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(f"Join messages will be sent to {channel.mention}")

    @joinmessage.command()
    async def message(self, ctx, *, message: str):
        """Set the join message template.
        
        Use {user} for user mention and {link} for the webpage link.
        """
        await self.config.guild(ctx.guild).message.set(message)
        await ctx.send("Join message template updated.")

    @joinmessage.command()
    async def link(self, ctx, link: str):
        """Set the webpage link to include in join messages."""
        await self.config.guild(ctx.guild).link.set(link)
        await ctx.send(f"Join message link set to: {link}")

    @joinmessage.command()
    async def settings(self, ctx):
        """Show current join message settings."""
        guild_config = await self.config.guild(ctx.guild).all()
        
        embed = discord.Embed(title="Join Message Settings", color=0x3498db)
        embed.add_field(name="Enabled", value=guild_config["enabled"], inline=True)
        
        if guild_config["channel"]:
            channel = ctx.guild.get_channel(guild_config["channel"])
            embed.add_field(name="Channel", value=channel.mention if channel else "Invalid channel", inline=True)
        else:
            embed.add_field(name="Channel", value="Not set", inline=True)
            
        embed.add_field(name="Link", value=guild_config["link"], inline=False)
        embed.add_field(name="Message Template", value=guild_config["message"], inline=False)
        
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Send welcome message when a member joins."""
        guild_config = await self.config.guild(member.guild).all()
        
        if not guild_config["enabled"]:
            return
            
        if not guild_config["channel"]:
            return
            
        channel = member.guild.get_channel(guild_config["channel"])
        if not channel:
            return
            
        message = guild_config["message"].format(
            user=member.mention,
            link=guild_config["link"]
        )
        
        try:
            await channel.send(message)
        except discord.HTTPException:
            pass