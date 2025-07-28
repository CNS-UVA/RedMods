from .joinmessage import JoinMessage

async def setup(bot):
    await bot.add_cog(JoinMessage(bot))