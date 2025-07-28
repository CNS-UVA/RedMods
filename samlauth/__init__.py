from .samlauth import SAMLAuth

async def setup(bot):
    await bot.add_cog(SAMLAuth(bot))