import asyncio
import json
import logging
import os
from typing import Dict, List, Optional, Set, Tuple

import asyncpg
import discord
from redbot.core import commands, Config, data_manager
from redbot.core.bot import Red

log = logging.getLogger("red.rolemanager")


class RoleManager(commands.Cog):
    """Advanced role management with dependencies and SAML integration."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=3456789012)
        default_guild = {
            "role_mappings": {},  # SAML attribute values -> Discord role IDs
            "role_dependencies": {},  # role_id -> [required_role_ids]
            "auto_assign": True,  # Automatically assign roles based on SAML data
            "sync_on_join": True,  # Sync roles when user joins
            "student_role_id": None,  # Role ID for students
            "faculty_staff_role_id": None,  # Role ID for faculty/staff/employees
            "alum_role_id": None  # Role ID for alumni
        }
        self.config.register_guild(**default_guild)
        
        # Database configuration for PostgreSQL
        self.db_config = {
            'host': os.getenv('POSTGRES_HOST', 'postgres'),
            'port': int(os.getenv('POSTGRES_PORT', '5432')),
            'database': os.getenv('POSTGRES_DB', 'redbot'),
            'user': os.getenv('POSTGRES_USER', 'redbot'),
            'password': os.getenv('POSTGRES_PASSWORD', '')
        }
        self.db_pool = None
        
        asyncio.create_task(self._setup_database())

    async def _setup_database(self):
        """Initialize the PostgreSQL database connection."""
        try:
            self.db_pool = await asyncpg.create_pool(**self.db_config)
            log.info("Role manager database connection established")
        except Exception as e:
            log.error(f"Failed to connect to database: {e}")
            self.db_pool = None

    async def _get_saml_user_data(self, discord_user_id: str) -> Optional[Dict]:
        """Retrieve SAML user data from the database."""
        if not self.db_pool:
            return None
        
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchrow(
                    'SELECT saml_nameid, attributes FROM saml_users WHERE discord_user_id = $1',
                    discord_user_id
                )
                
            if result:
                return {
                    'saml_nameid': result['saml_nameid'],
                    'attributes': json.loads(result['attributes']) if isinstance(result['attributes'], str) else result['attributes']
                }
        except Exception as e:
            log.error(f"Failed to get SAML user data: {e}")
            
        return None

    async def _get_roles_for_user(self, guild: discord.Guild, user_data: Dict) -> List[discord.Role]:
        """Determine which roles a user should have based on their SAML attributes with priority logic."""
        guild_config = await self.config.guild(guild).all()
        
        # Get the OID attribute list
        attributes = user_data.get('attributes', {})
        oid_list = attributes.get('urn:oid:1.3.6.1.4.1.5923.1.1.1.1', [])
        
        if not isinstance(oid_list, list):
            oid_list = [oid_list] if oid_list else []
        
        # Convert to lowercase for case-insensitive matching
        oid_values = [str(value).lower() for value in oid_list]
        
        roles_to_assign = []
        
        # Priority-based role assignment (exclusive clauses)
        if 'student' in oid_values:
            # Student role (highest priority)
            student_role_id = guild_config.get('student_role_id')
            if student_role_id:
                student_role = guild.get_role(student_role_id)
                if student_role:
                    roles_to_assign.append(student_role)
        elif any(val in oid_values for val in ['faculty', 'staff', 'employee']):
            # Faculty and Staff role (second priority)
            faculty_staff_role_id = guild_config.get('faculty_staff_role_id')
            if faculty_staff_role_id:
                faculty_staff_role = guild.get_role(faculty_staff_role_id)
                if faculty_staff_role:
                    roles_to_assign.append(faculty_staff_role)
        elif 'alum' in oid_values:
            # Alum role (third priority)
            alum_role_id = guild_config.get('alum_role_id')
            if alum_role_id:
                alum_role = guild.get_role(alum_role_id)
                if alum_role:
                    roles_to_assign.append(alum_role)
        
        # Also check for any additional custom role mappings
        role_mappings = guild_config['role_mappings']
        for attribute_key, role_mapping in role_mappings.items():
            if attribute_key in attributes:
                user_attribute_values = attributes[attribute_key]
                if not isinstance(user_attribute_values, list):
                    user_attribute_values = [user_attribute_values]
                
                for value in user_attribute_values:
                    if value in role_mapping:
                        role_id = role_mapping[value]
                        role = guild.get_role(role_id)
                        if role and role not in roles_to_assign:
                            roles_to_assign.append(role)
        
        return roles_to_assign

    async def _check_role_dependencies(self, guild: discord.Guild, member: discord.Member, 
                                     target_roles: Set[discord.Role]) -> Tuple[Set[discord.Role], Set[discord.Role]]:
        """Check role dependencies and return roles to add and remove."""
        guild_config = await self.config.guild(guild).all()
        role_dependencies = guild_config['role_dependencies']
        
        current_roles = set(member.roles[1:])  # Exclude @everyone
        roles_to_add = set()
        roles_to_remove = set()
        
        # Check which roles should be added based on dependencies
        for role in target_roles:
            role_id = str(role.id)
            if role_id in role_dependencies:
                required_role_ids = role_dependencies[role_id]
                required_roles = [guild.get_role(int(rid)) for rid in required_role_ids]
                required_roles = [r for r in required_roles if r]  # Filter out None values
                
                # Check if user has all required roles
                if all(req_role in current_roles or req_role in target_roles for req_role in required_roles):
                    roles_to_add.add(role)
            else:
                # No dependencies, can add directly
                roles_to_add.add(role)
        
        # Check which roles should be removed due to dependency violations
        for role in current_roles:
            if role not in target_roles:
                # Check if any role depends on this one
                for dep_role_id, required_role_ids in role_dependencies.items():
                    if str(role.id) in required_role_ids:
                        dep_role = guild.get_role(int(dep_role_id))
                        if dep_role and dep_role in current_roles and dep_role not in target_roles:
                            roles_to_remove.add(dep_role)
        
        # Remove roles that don't meet dependencies
        final_roles_to_add = set()
        for role in roles_to_add:
            role_id = str(role.id)
            if role_id in role_dependencies:
                required_role_ids = role_dependencies[role_id]
                required_roles = [guild.get_role(int(rid)) for rid in required_role_ids]
                required_roles = [r for r in required_roles if r]
                
                if all(req_role in current_roles or req_role in roles_to_add for req_role in required_roles):
                    final_roles_to_add.add(role)
            else:
                final_roles_to_add.add(role)
        
        return final_roles_to_add, roles_to_remove

    async def sync_user_roles(self, member: discord.Member) -> bool:
        """Sync a user's roles based on their SAML authentication data."""
        user_data = await self._get_saml_user_data(str(member.id))
        if not user_data:
            return False
        
        target_roles = await self._get_roles_for_user(member.guild, user_data)
        target_roles_set = set(target_roles)
        
        roles_to_add, roles_to_remove = await self._check_role_dependencies(
            member.guild, member, target_roles_set
        )
        
        # Apply role changes
        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Role dependency enforcement")
                log.info(f"Removed roles {[r.name for r in roles_to_remove]} from {member}")
            
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="SAML role synchronization")
                log.info(f"Added roles {[r.name for r in roles_to_add]} to {member}")
            
            return True
        except discord.HTTPException as e:
            log.error(f"Failed to sync roles for {member}: {e}")
            return False

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(manage_roles=True)
    async def rolemanager(self, ctx):
        """Configure role management settings."""
        pass

    @rolemanager.group()
    async def mapping(self, ctx):
        """Configure SAML attribute to role mappings."""
        pass

    @mapping.command(name="add")
    async def mapping_add(self, ctx, attribute: str, value: str, role: discord.Role):
        """Add a SAML attribute value to role mapping.
        
        Examples:
        `[p]rolemanager mapping add "urn:oid:1.3.6.1.4.1.5923.1.1.1.1" "user123" @MemberRole`
        `[p]rolemanager mapping add groups "Admin" @AdminRole`
        """
        guild_config = await self.config.guild(ctx.guild).all()
        role_mappings = guild_config['role_mappings']
        
        if attribute not in role_mappings:
            role_mappings[attribute] = {}
        
        role_mappings[attribute][value] = role.id
        await self.config.guild(ctx.guild).role_mappings.set(role_mappings)
        
        # Show a cleaner display name for OID attributes
        display_attr = attribute.split(':')[-1] if attribute.startswith('urn:oid:') else attribute
        await ctx.send(f"Added mapping: SAML attribute `{display_attr}` = `{value}` → {role.mention}")

    @mapping.command(name="remove")
    async def mapping_remove(self, ctx, attribute: str, value: str):
        """Remove a SAML attribute value to role mapping."""
        guild_config = await self.config.guild(ctx.guild).all()
        role_mappings = guild_config['role_mappings']
        
        if attribute in role_mappings and value in role_mappings[attribute]:
            del role_mappings[attribute][value]
            if not role_mappings[attribute]:  # Remove empty attribute mapping
                del role_mappings[attribute]
            
            await self.config.guild(ctx.guild).role_mappings.set(role_mappings)
            await ctx.send(f"Removed mapping for `{attribute}` = `{value}`")
        else:
            await ctx.send("Mapping not found.")

    @mapping.command(name="list")
    async def mapping_list(self, ctx):
        """List all SAML attribute to role mappings."""
        guild_config = await self.config.guild(ctx.guild).all()
        role_mappings = guild_config['role_mappings']
        
        if not role_mappings:
            await ctx.send("No role mappings configured.")
            return
        
        embed = discord.Embed(title="SAML Role Mappings", color=0x3498db)
        
        for attribute, value_mappings in role_mappings.items():
            mapping_text = []
            for value, role_id in value_mappings.items():
                role = ctx.guild.get_role(role_id)
                role_mention = role.mention if role else f"<deleted role {role_id}>"
                mapping_text.append(f"`{value}` → {role_mention}")
            
            # Show cleaner display name for OID attributes
            display_attr = attribute.split(':')[-1] if attribute.startswith('urn:oid:') else attribute
            full_attr = f"{display_attr}\n({attribute})" if attribute.startswith('urn:oid:') else attribute
            
            embed.add_field(
                name=f"Attribute: {full_attr}",
                value="\n".join(mapping_text),
                inline=False
            )
        
        await ctx.send(embed=embed)

    @mapping.command(name="addoid")
    async def mapping_add_oid(self, ctx, oid_value: str, role: discord.Role):
        """Add a mapping for the main OID attribute (1.3.6.1.4.1.5923.1.1.1.1).
        
        This is a convenience command for the most common OID attribute.
        Example: `[p]rolemanager mapping addoid "user123" @MemberRole`
        """
        oid_attribute = "urn:oid:1.3.6.1.4.1.5923.1.1.1.1"
        guild_config = await self.config.guild(ctx.guild).all()
        role_mappings = guild_config['role_mappings']
        
        if oid_attribute not in role_mappings:
            role_mappings[oid_attribute] = {}
        
        role_mappings[oid_attribute][oid_value] = role.id
        await self.config.guild(ctx.guild).role_mappings.set(role_mappings)
        
        await ctx.send(f"Added OID mapping: `{oid_value}` → {role.mention}")

    @mapping.command(name="removeoid")
    async def mapping_remove_oid(self, ctx, oid_value: str):
        """Remove a mapping for the main OID attribute."""
        oid_attribute = "urn:oid:1.3.6.1.4.1.5923.1.1.1.1"
        guild_config = await self.config.guild(ctx.guild).all()
        role_mappings = guild_config['role_mappings']
        
        if oid_attribute in role_mappings and oid_value in role_mappings[oid_attribute]:
            del role_mappings[oid_attribute][oid_value]
            if not role_mappings[oid_attribute]:  # Remove empty attribute mapping
                del role_mappings[oid_attribute]
            
            await self.config.guild(ctx.guild).role_mappings.set(role_mappings)
            await ctx.send(f"Removed OID mapping for `{oid_value}`")
        else:
            await ctx.send("OID mapping not found.")

    @rolemanager.group()
    async def dependency(self, ctx):
        """Configure role dependencies."""
        pass

    @dependency.command(name="add")
    async def dependency_add(self, ctx, target_role: discord.Role, required_role: discord.Role):
        """Add a role dependency.
        
        Users must have the required role to get the target role.
        """
        guild_config = await self.config.guild(ctx.guild).all()
        role_dependencies = guild_config['role_dependencies']
        
        target_id = str(target_role.id)
        if target_id not in role_dependencies:
            role_dependencies[target_id] = []
        
        required_id = str(required_role.id)
        if required_id not in role_dependencies[target_id]:
            role_dependencies[target_id].append(required_id)
            await self.config.guild(ctx.guild).role_dependencies.set(role_dependencies)
            await ctx.send(f"Added dependency: {target_role.mention} requires {required_role.mention}")
        else:
            await ctx.send("This dependency already exists.")

    @dependency.command(name="remove")
    async def dependency_remove(self, ctx, target_role: discord.Role, required_role: discord.Role):
        """Remove a role dependency."""
        guild_config = await self.config.guild(ctx.guild).all()
        role_dependencies = guild_config['role_dependencies']
        
        target_id = str(target_role.id)
        required_id = str(required_role.id)
        
        if target_id in role_dependencies and required_id in role_dependencies[target_id]:
            role_dependencies[target_id].remove(required_id)
            if not role_dependencies[target_id]:  # Remove empty dependency list
                del role_dependencies[target_id]
            
            await self.config.guild(ctx.guild).role_dependencies.set(role_dependencies)
            await ctx.send(f"Removed dependency: {target_role.mention} no longer requires {required_role.mention}")
        else:
            await ctx.send("Dependency not found.")

    @dependency.command(name="list")
    async def dependency_list(self, ctx):
        """List all role dependencies."""
        guild_config = await self.config.guild(ctx.guild).all()
        role_dependencies = guild_config['role_dependencies']
        
        if not role_dependencies:
            await ctx.send("No role dependencies configured.")
            return
        
        embed = discord.Embed(title="Role Dependencies", color=0x3498db)
        
        for target_role_id, required_role_ids in role_dependencies.items():
            target_role = ctx.guild.get_role(int(target_role_id))
            target_name = target_role.mention if target_role else f"<deleted role {target_role_id}>"
            
            required_names = []
            for req_id in required_role_ids:
                req_role = ctx.guild.get_role(int(req_id))
                req_name = req_role.mention if req_role else f"<deleted role {req_id}>"
                required_names.append(req_name)
            
            embed.add_field(
                name=target_name,
                value=f"Requires: {', '.join(required_names)}",
                inline=False
            )
        
        await ctx.send(embed=embed)

    @rolemanager.command()
    async def sync(self, ctx, member: discord.Member = None):
        """Manually sync roles for a user based on their SAML data."""
        if member is None:
            member = ctx.author
        
        if not ctx.author.guild_permissions.manage_roles and member != ctx.author:
            await ctx.send("You don't have permission to sync other users' roles.")
            return
        
        success = await self.sync_user_roles(member)
        if success:
            await ctx.send(f"Successfully synced roles for {member.mention}")
        else:
            await ctx.send(f"No SAML data found for {member.mention} or sync failed.")

    @rolemanager.command()
    async def syncall(self, ctx):
        """Sync roles for all members with SAML authentication data."""
        if not ctx.author.guild_permissions.manage_roles:
            await ctx.send("You don't have permission to sync roles.")
            return
        
        if not self.db_pool:
            await ctx.send("Database not available.")
            return
        
        try:
            async with self.db_pool.acquire() as conn:
                user_ids = await conn.fetch('SELECT discord_user_id FROM saml_users')
            
            synced_count = 0
            failed_count = 0
            
            for user_record in user_ids:
                user_id = user_record['discord_user_id']
                member = ctx.guild.get_member(int(user_id))
                if member:
                    success = await self.sync_user_roles(member)
                    if success:
                        synced_count += 1
                    else:
                        failed_count += 1
            
            await ctx.send(f"Role sync complete: {synced_count} users synced, {failed_count} failed.")
        except Exception as e:
            log.error(f"Failed to sync all users: {e}")
            await ctx.send("Failed to retrieve user list for sync.")

    @rolemanager.command()
    async def settings(self, ctx):
        """Show current role manager settings."""
        guild_config = await self.config.guild(ctx.guild).all()
        
        embed = discord.Embed(title="Role Manager Settings", color=0x3498db)
        embed.add_field(name="Auto Assign", value=guild_config["auto_assign"], inline=True)
        embed.add_field(name="Sync on Join", value=guild_config["sync_on_join"], inline=True)
        embed.add_field(name="Role Mappings", value=len(guild_config["role_mappings"]), inline=True)
        embed.add_field(name="Role Dependencies", value=len(guild_config["role_dependencies"]), inline=True)
        
        await ctx.send(embed=embed)

    @rolemanager.group()
    async def priorityroles(self, ctx):
        """Configure priority-based roles for OID attributes."""
        pass

    @priorityroles.command()
    async def student(self, ctx, role: discord.Role = None):
        """Set or view the Student role."""
        if role is None:
            current_role_id = await self.config.guild(ctx.guild).student_role_id()
            if current_role_id:
                current_role = ctx.guild.get_role(current_role_id)
                role_mention = current_role.mention if current_role else f"<deleted role {current_role_id}>"
                await ctx.send(f"Current Student role: {role_mention}")
            else:
                await ctx.send("No Student role configured.")
        else:
            await self.config.guild(ctx.guild).student_role_id.set(role.id)
            await ctx.send(f"Student role set to {role.mention}")

    @priorityroles.command()
    async def facultystaff(self, ctx, role: discord.Role = None):
        """Set or view the Faculty/Staff role."""
        if role is None:
            current_role_id = await self.config.guild(ctx.guild).faculty_staff_role_id()
            if current_role_id:
                current_role = ctx.guild.get_role(current_role_id)
                role_mention = current_role.mention if current_role else f"<deleted role {current_role_id}>"
                await ctx.send(f"Current Faculty/Staff role: {role_mention}")
            else:
                await ctx.send("No Faculty/Staff role configured.")
        else:
            await self.config.guild(ctx.guild).faculty_staff_role_id.set(role.id)
            await ctx.send(f"Faculty/Staff role set to {role.mention}")

    @priorityroles.command()
    async def alum(self, ctx, role: discord.Role = None):
        """Set or view the Alumni role."""
        if role is None:
            current_role_id = await self.config.guild(ctx.guild).alum_role_id()
            if current_role_id:
                current_role = ctx.guild.get_role(current_role_id)
                role_mention = current_role.mention if current_role else f"<deleted role {current_role_id}>"
                await ctx.send(f"Current Alumni role: {role_mention}")
            else:
                await ctx.send("No Alumni role configured.")
        else:
            await self.config.guild(ctx.guild).alum_role_id.set(role.id)
            await ctx.send(f"Alumni role set to {role.mention}")

    @priorityroles.command(name="list")
    async def priorityroles_list(self, ctx):
        """List all configured priority roles."""
        guild_config = await self.config.guild(ctx.guild).all()
        
        embed = discord.Embed(title="Priority Role Configuration", color=0x3498db)
        embed.description = "Roles assigned based on OID attribute values (priority order):"
        
        # Student role
        student_role_id = guild_config.get('student_role_id')
        if student_role_id:
            student_role = ctx.guild.get_role(student_role_id)
            student_mention = student_role.mention if student_role else f"<deleted role {student_role_id}>"
        else:
            student_mention = "Not configured"
        embed.add_field(name="1. Student", value=f"{student_mention}\nTrigger: `student`", inline=False)
        
        # Faculty/Staff role
        faculty_staff_role_id = guild_config.get('faculty_staff_role_id')
        if faculty_staff_role_id:
            faculty_staff_role = ctx.guild.get_role(faculty_staff_role_id)
            faculty_staff_mention = faculty_staff_role.mention if faculty_staff_role else f"<deleted role {faculty_staff_role_id}>"
        else:
            faculty_staff_mention = "Not configured"
        embed.add_field(name="2. Faculty/Staff", value=f"{faculty_staff_mention}\nTriggers: `faculty`, `staff`, `employee`", inline=False)
        
        # Alumni role
        alum_role_id = guild_config.get('alum_role_id')
        if alum_role_id:
            alum_role = ctx.guild.get_role(alum_role_id)
            alum_mention = alum_role.mention if alum_role else f"<deleted role {alum_role_id}>"
        else:
            alum_mention = "Not configured"
        embed.add_field(name="3. Alumni", value=f"{alum_mention}\nTrigger: `alum`", inline=False)
        
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Sync roles when a member joins the server."""
        guild_config = await self.config.guild(member.guild).all()
        if guild_config['sync_on_join']:
            await asyncio.sleep(2)  # Brief delay to allow other systems to process
            await self.sync_user_roles(member)

    def cog_unload(self):
        """Clean up when the cog is unloaded."""
        if self.db_pool:
            asyncio.create_task(self.db_pool.close())