# Mesho Cleaner

Telegram group moderation bot (starter version).

## Features
- Link deletion (configurable by group admins)
- Repeated-text spam deletion (in-memory)
- AI image moderation for photos and static stickers (requires OpenAI API key)
- Admins are exempt from moderation
- Telegram webhook deployment on Render

## Commands
- `/settings`
- `/links_on`, `/links_off`
- `/media_on`, `/media_off`
- `/spam_on`, `/spam_off`

## Required environment variables
- `BOT_TOKEN`: token from @BotFather
- `OWNER_ID`: your Telegram numeric user ID
- `WEBHOOK_SECRET`: random secret, 20+ characters recommended
- `OPENAI_API_KEY`: API key for image moderation

## Deploy
Create a Render Web Service from this repository.
Build command: `pip install -r requirements.txt`
Start command: `uvicorn bot:app --host 0.0.0.0 --port $PORT`

Render automatically provides `RENDER_EXTERNAL_URL`.

## Important limitations
- Add the bot as a group administrator and grant Delete Messages.
- The bot cannot inspect the full historical member list.
- This version moderates photos and static stickers. Animated stickers, GIFs, and videos are not analyzed in this starter version.
- Anti-spam history and group settings are in memory and reset after a restart/redeploy.
- AI moderation can make mistakes. Test in a private test group before using in a live community.
- Use only in groups where you have permission to moderate content.
