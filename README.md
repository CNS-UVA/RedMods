# Red-DiscordBot CNS Cogs

Red-DiscordBot cogs for user authentication and role management using SAML 2.0.

## Features

### JoinMessage Cog
- Send welcome messages with configurable webpage links when users join
- Customizable message templates with user mentions and links
- Channel-specific configuration

### SAMLAuth Cog  
- SAML 2.0 service provider functionality
- Web server for handling SAML authentication flows
- Database storage with expiration management (365-day reminders, 395-day expiration)
- Automatic cleanup of expired entries

### RoleManager Cog
- Priority-based role assignment from SAML attributes
- Hierarchical role dependencies
- Automatic role synchronization
- Support for OID attributes

## Quick Start with Docker

1. **Clone and setup**:
   ```bash
   git clone <repository-url>
   cd RedMods
   cp .env.example .env
   cp saml.json.example saml.json
   ```

2. **Configure environment**:
   Edit `.env` with your Discord bot token and database password.

3. **Configure SAML**:
   Edit `saml.json` with your SAML provider settings.

4. **Start services**:
   ```bash
   docker-compose up -d
   ```

5. **Load cogs** (in Discord):
   ```
   !load joinmessage
   !load samlauth  
   !load rolemanager
   ```

## Configuration

### SAML Authentication Setup

1. **Configure priority roles**:
   ```
   !rolemanager priorityroles student @Student
   !rolemanager priorityroles facultystaff @Faculty
   !rolemanager priorityroles alum @Alumni
   ```

2. **Enable SAML auth**:
   ```
   !samlauth enable
   ```

3. **Link users manually** (if needed):
   ```
   !samlauth link @user student faculty
   ```

### Role Assignment Priority

The system assigns roles based on OID attribute values with exclusive priority:

1. **Student** (highest): If OID contains `student`
2. **Faculty/Staff**: If OID contains `faculty`, `staff`, or `employee`  
3. **Alumni** (lowest): If OID contains `alum`

### Expiration Management

- **Verification**: Records when users authenticate via SAML
- **Reminder**: 365 days after verification
- **Expiration**: 395 days after verification  
- **Cleanup**: Automatic daily removal of expired entries

## Commands

### JoinMessage
- `!joinmessage enable/disable` - Toggle join messages
- `!joinmessage channel #channel` - Set message channel
- `!joinmessage message <template>` - Set message template
- `!joinmessage link <url>` - Set webpage link

### SAMLAuth  
- `!samlauth enable/disable` - Toggle SAML authentication
- `!samlauth link @user <oid_values>` - Manually link user to OID values
- `!samlauth cleanup` - Remove expired entries
- `!samlauth reminders` - Show users needing verification

### RoleManager
- `!rolemanager sync @user` - Manually sync user roles
- `!rolemanager mapping add <attribute> <value> @role` - Add role mapping
- `!rolemanager dependency add @target_role @required_role` - Add role dependency
- `!rolemanager priorityroles student @role` - Configure priority roles

## SAML Endpoints

When the web server is running, these endpoints are available:

- `GET /login` - Initiate SAML login
- `GET/POST /?acs` - Assertion Consumer Service  
- `GET/POST /?sls` - Single Logout Service
- `GET /metadata/` - Service Provider metadata

## Environment Variables

- `DISCORD_TOKEN` - Discord bot token
- `REDBOT_PREFIX` - Command prefix (default: `!`)
- `SAML_PORT` - SAML web server port (default: 6969)
- `POSTGRES_*` - Database configuration

## Development

### Local Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Install Red-DiscordBot:
   ```bash
   pip install Red-DiscordBot
   ```

3. Setup Red instance:
   ```bash
   redbot-setup
   ```

4. Install cogs:
   ```bash
   redbot <instance> --load-cogs-path ./
   ```

### File Structure

```
RedMods/
├── joinmessage/          # Join message cog
├── samlauth/             # SAML authentication cog  
├── rolemanager/          # Role management cog
├── docker-compose.yml    # Docker services
├── Dockerfile           # Bot container
├── requirements.txt     # Python dependencies
├── saml.json           # SAML configuration
└── README.md           # This file
```

## Security Notes

- Keep `saml.json` secure and use proper certificates in production
- Use strong database passwords  
- Consider running behind a reverse proxy with TLS
- Regularly review user access and role assignments

## Support

For issues or questions, please check the cog documentation or create an issue in the repository.