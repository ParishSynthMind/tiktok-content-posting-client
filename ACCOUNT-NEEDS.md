# What is needed for the live TikTok test

Everything except the real post has been built and tested. The last step needs a free TikTok for Developers app, which only the account owner can create.

**Start this early: TikTok's app review takes days, not minutes.** TikTok publishes no timeline, and there are two reviews:

1. **App review**, before the app can be used outside sandbox.
2. **Content Posting audit**, before posts can be anything other than private (SELF_ONLY).

You can test in **sandbox** straight away while those are pending.

## What you (the account owner) do, about 20 minutes, free

1. **Open** https://developers.tiktok.com/ and log in with your TikTok account. Accept the developer terms if asked. This step is yours, not mine.
2. **Create the app.** Go to https://developers.tiktok.com/apps/ and choose **Connect an app**, then fill in:
   - **App icon:** a square image, 1024 x 1024
   - **App name:** `Content Posting Client`
   - **Category:** Productivity (or Business)
   - **Description:** "Lets me post my own videos to my own TikTok account after I approve each caption."
   - **Terms of Service URL:** `https://parishsynthmind.github.io/tiktok-content-posting-client/terms.html`
   - **Privacy Policy URL:** `https://parishsynthmind.github.io/tiktok-content-posting-client/privacy.html`
   - **Platforms:** tick **Web**. Website URL: `https://parishsynthmind.github.io/tiktok-content-posting-client/`
   - If TikTok asks to verify that website, tell me. I will add the verification file it gives you.
3. **Add products** (Add products section on the app page):
   - **Login Kit:** add the Redirect URI `https://parishsynthmind.github.io/tiktok-content-posting-client/callback/` exactly. TikTok only accepts https addresses, which is why it is a web page and not localhost.
   - **Content Posting API:** switch on **Direct Post**.
   - **Scopes:** make sure the app has `user.info.basic`, `video.upload` and `video.publish`.
4. **Sandbox, so you can test today:**
   - Turn on Sandbox at the top of the app page and create a sandbox.
   - Under **Target users**, press **Add account** and log in with the TikTok account to test on. It can take up to an hour to show.
5. **Submit for review** (the Submit for review button). TikTok wants a short screen recording of the flow. I will make it from the sandbox test.
   - After that, apply for the Content Posting audit at https://developers.tiktok.com/application/content-posting-api so posts can be public.
6. **Save the app keys in the Keychain yourself.** Never paste them into chat or email. In Terminal, run each line. It asks for the value, so it never appears on screen:
   ```
   security add-generic-password -a neo -s tiktok-client-key -w
   security add-generic-password -a neo -s tiktok-client-secret -w
   ```
   Paste the Client key, then the Client secret, from the app page. Use the sandbox keys while testing in sandbox.

## The live test, once the sandbox app exists

1. `python -m tt_poster auth`: TikTok's sign-in page opens and you press **Authorize**. You then land on the "Sign-in finished" page; copy its address into the terminal. The tokens are saved to the Keychain.
2. `python -m tt_poster whoami`: shows the account, the privacy options TikTok offers it, and the longest video it may post.
3. Draft a caption for a real short clip, then approve it with `--privacy SELF_ONLY --agree-music-terms`. Run `publish --dry-run` first, then `publish`. The tool uploads the clip, polls the status until `PUBLISH_COMPLETE`, and the video shows in the app as **Only me**. Delete it afterwards if you like.
4. Run `publish` again to prove the duplicate guard skips it, and check `queue/posted-log.jsonl`.

**Important for step 3:** until the audit passes, TikTok also requires the account to be set to **private** at the moment of posting. If your real channel is public, either:

- use a spare TikTok account as the sandbox target user, or
- try `--mode inbox` first, which puts the clip in your TikTok inbox as a draft for you to finish or discard in the app.

If TikTok still refuses, the account goes private for five minutes during the test and back to public straight after.

## Good to know

- Access tokens last about 2 hours and refresh themselves. You sign in again only every 180 days or so.
- TikTok caps posting at about 15 posts a day per account, and 6 post starts a minute per user. This tool is far below both.
