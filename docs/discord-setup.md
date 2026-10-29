# Discord speaker names: setup

VoiceRhino can label captions with the real Discord names of the people talking, and
give each person their own color. It asks the Discord app on your PC who is speaking
right now (the same green ring you see in Discord) and matches that to the captions.

To do that, Discord needs you to create a small "application" of your own in the Discord
Developer Portal. It's free, takes about five minutes, and you only do it once.

## What you need

- The **Discord desktop app** running on the same PC as VoiceRhino. Discord in a web
  browser won't work.
- The Discord account you use in that app. Use the same account to create the
  application below.

## 1. Create the application

1. Open <https://discord.com/developers/applications> and log in with your Discord account.
2. Click **New Application**. Name it `VoiceRhino`, agree to the terms, and click
   **Create**. Discord shows this name in the approval popup and under Authorized Apps.
3. In the left menu, click **OAuth2**.
4. Under **Client information**, copy the **Client ID**. It is the same number as the
   Application ID on the General Information page.
5. Under **Client Secret**, click **Reset Secret**, confirm, and copy the secret. Discord
   shows it only once. Treat it like a password: don't post it or put it in screenshots.
   If you lose it, reset it again and paste the new one into VoiceRhino.
6. Still on the OAuth2 page, under **Redirects**, click **Add Redirect** and enter exactly:

   ```
   http://localhost
   ```

   Use no trailing slash and no port number. Then click **Save Changes**.

You can leave everything else alone. You don't need a bot, RPC Origins, or Public
Client.

## 2. Connect VoiceRhino

1. Right-click VoiceRhino's **CC** tray icon → **Settings… → Speakers**. Paste the
   **Client ID** and **Client Secret**, tick **Use Discord names**, and click **Apply**.
   (VoiceRhino always uses the `http://localhost` redirect from step 1.6.)
2. Switch to Discord. **The first time only**, a popup appears inside the Discord app
   asking you to authorize your `VoiceRhino` app. It lists what the app may do, such as
   seeing your username and reading voice events. Click **Authorize**. While Discord is
   waiting for you, VoiceRhino's status reads *Waiting for approval in Discord*.
3. Join a voice channel. The status changes to something like
   *Connected: #General (4 people)*, and names start appearing next to captions.

After that, you won't see the popup again. VoiceRhino saves the sign-in and renews it
automatically. Discord's sign-ins last 7 days and come with a renewal token.

## Where the sign-in is stored

VoiceRhino saves the sign-in in a file on your PC:

```
%APPDATA%\VoiceRhino\discord_token.json
```

If the `VOICERHINO_CONFIG` environment variable is set, the file is in that folder
instead.

The file contains the access token and the refresh token. Your client secret is not in
it. Don't share this file. Deleting it makes VoiceRhino ask Discord for approval again.

VoiceRhino only goes online to `discord.com` to swap the one-time approval code for a
token and to renew that token. Speaking events and names come straight from the Discord
app on your PC and stay there.

## How to revoke access

- In Discord, open **User Settings > Authorized Apps**, find `VoiceRhino`, and click
  **Deauthorize**. This turns off the saved sign-in immediately.
- Also delete `%APPDATA%\VoiceRhino\discord_token.json`.
- If you think your client secret leaked, click **Reset Secret** on the OAuth2 page of
  the Developer Portal. You can also delete the whole application there.

## Does Discord need to approve my app?

**No, not for you, as long as you use the account that created the application.**

Discord's documentation lists the permissions this feature uses (`rpc` and
`rpc.voice.read`) as "only available to approved partners". The same documentation says
that unapproved apps may still use RPC for "a list of approved testers"
([RPC docs][rpc], [OAuth2 scopes][oauth2]).

In practice, these accounts can authorize an unreviewed app:

- the account that owns the application
- members of its developer team
- accepted App Testers

This is what Discord tools that have you create your own app rely on, for example
[StreamDeck-Discord][streamdeck]. In a [public test][mcp-test], other accounts were
refused with RPC error 5000 `invalid_scope`, and an account worked once it accepted an
App Tester invite.

The owner case is community-observed, not a sentence in Discord's docs. We haven't
verified it ourselves with a real account yet.

If a friend wants speaker names too, they have two options:

- **Simplest:** they create their own application with the steps above.
- **Use yours:** in the Developer Portal, open **App Testers** in the left menu and invite
  their Discord username. Discord allows up to 50 testers. They must accept the invite
  from their email ([Discord developer support][testers]). They would also need your
  client secret, so the first option is usually better.

## Limitations

- **Only your current voice channel.** VoiceRhino follows whichever voice channel or call
  your own Discord app is in. It switches automatically when you move, and stops when
  you leave.
- **Desktop app only.** It talks to Discord through a local named pipe
  (`\\.\pipe\discord-ipc-0` and up). If you run several Discord versions at once
  (Stable, PTB, Canary), VoiceRhino uses the first one it finds.
- **Discord's speaking light, not voice recognition.** VoiceRhino knows who Discord says
  is talking, and matches that to captions by time. When two people talk at once, it
  picks whoever talked most during that caption. Bots that play audio count as speakers.
  Discord's indicator flickers briefly within sentences, and VoiceRhino smooths that
  out.
- **You are left out.** VoiceRhino captions your speakers, and they never contain your
  own microphone, so your own speaking light is ignored.
- **Same admin level.** If Discord runs as administrator and VoiceRhino doesn't, or the
  other way round, Windows blocks the connection. Run both normally.
- Discord limits how quickly an app can reconnect, about twice a minute. After Discord
  restarts, VoiceRhino may take up to a minute to reconnect.

## Status messages

| Status | What to do |
|---|---|
| Discord not running | Start the Discord desktop app. |
| Waiting for approval in Discord | Look for the authorization popup in the Discord window and click **Authorize**. |
| Connected: not in a voice channel | Everything works. Join a voice channel or call. |
| Discord doesn't recognise the client ID | Paste the Client ID (Application ID) from the OAuth2 page again. |
| Discord rejected the client secret | Click **Reset Secret** in the portal and paste the new secret. |
| Discord rejected the redirect URI | Add `http://localhost` under **OAuth2 > Redirects** and click **Save Changes**. |
| Authorization was declined in Discord | Turn the Discord feature off and on to see the popup again. |
| This Discord account can't use the app | Use the Discord account that created the app, or add this account as an App Tester. |
| Can't open Discord's pipe | Run Discord and VoiceRhino at the same admin level. |

## Sources

- Discord RPC documentation: IPC transport, AUTHORIZE / AUTHENTICATE, voice events, and
  the testers rule. <https://docs.discord.com/developers/topics/rpc>
- Discord OAuth2 documentation: the `rpc` and `rpc.voice.read` scopes, the token
  exchange, refresh, and revoke. <https://docs.discord.com/developers/topics/oauth2>
- Discord developer support on App Testers: the left-menu page, the limit of 50, and the
  email invite. <https://support-dev.discord.com/hc/en-us/articles/21204493235991-How-Can-My-Friends-Play-My-Activity>
- Community experiment: an accepted App Tester can authorize RPC scopes, and other
  accounts get `invalid_scope`. <https://github.com/ver-1000000/discord-rpc-mcp/issues/1>
- StreamDeck-Discord: users create their own application with a localhost redirect,
  client ID, and secret. <https://github.com/fredemmott/StreamDeck-Discord>
- Discord Userdoccers (unofficial): RPC payload details, such as `channel_id` on speaking
  events, and the limit of 2 connections per minute.
  <https://docs.discord.food/topics/rpc>

[rpc]: https://docs.discord.com/developers/topics/rpc
[oauth2]: https://docs.discord.com/developers/topics/oauth2
[testers]: https://support-dev.discord.com/hc/en-us/articles/21204493235991-How-Can-My-Friends-Play-My-Activity
[mcp-test]: https://github.com/ver-1000000/discord-rpc-mcp/issues/1
[streamdeck]: https://github.com/fredemmott/StreamDeck-Discord
