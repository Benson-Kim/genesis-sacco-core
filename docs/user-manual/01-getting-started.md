# 1. Getting started

This chapter shows you how to sign in, what to do if your sign-in code does
not arrive, and how to sign out safely.

## Signing in

Genesis Prestige does not use passwords. Instead, every time you sign in you
receive a fresh **one-time code**. A code works once, and only for a few
minutes, so a stolen code is useless to anyone else.

**What you need before you start**

- The web address of your SACCO's console (ask your administrator).
- The email address **or** the mobile phone number your administrator
  registered for you.

**Steps**

1. Open the console address in your browser. The sign-in screen appears.

   ![SCREENSHOT: The sign-in screen with the "Email or phone" field empty](./images/PLACEHOLDER-login-screen.png)
   <!-- TODO(screenshot): capture the /login screen in a fresh browser session, no user signed in, before anything is typed -->

2. Type your email address, or your mobile number. Phone numbers can be
   typed the way you are used to — for example `0712345678` or
   `+254712345678`. The screen tells you as you type if the number does not
   look right.
3. Select **the button to request your code**. You will see a confirmation
   that a one-time code has been sent.

   ![SCREENSHOT: The sign-in screen after requesting a code, showing the six-digit code entry](./images/PLACEHOLDER-login-code-entry.png)
   <!-- TODO(screenshot): capture the /login screen immediately after requesting a code, with the 6-digit code inputs visible, any staff role -->

4. Enter the **six-digit code** you received. You can also paste the whole
   code at once.
5. You are signed in and land on the Dashboard.

   ![SCREENSHOT: The dashboard right after signing in](./images/PLACEHOLDER-dashboard-after-login.png)
   <!-- TODO(screenshot): capture the /dashboard screen signed in as a Branch Manager, with the sidebar fully visible -->

**What happens next**

You stay signed in while you work. For your protection the session is
short-lived and renews itself quietly in the background; if you stay away
for a long time you may be asked to sign in again.

> ℹ️ **Pilot environments.** While message delivery is still being set up,
> some test environments show the code directly on the sign-in screen after
> you request it. This is for testing only and will not happen in live use.

## If the code does not arrive

1. Wait a minute — delivery can lag.
2. Check that you typed the same email or phone number your administrator
   registered. The system deliberately does not tell you whether an address
   is registered, so a silent non-arrival can simply mean a typo.
3. Request a new code and use the newest one. Older codes stop working as
   soon as a newer one exists.
4. Still nothing? Contact your administrator and ask them to confirm the
   email and phone number on your account.

**Good to know about codes**

- A code expires **5 minutes** after it is sent.
- You get **5 attempts** to type a code. After that the code locks and you
  must request a new one.
- A code works **once**. If you sign in and then need to sign in again, you
  will need a fresh code.
- Too many requests in a short time are refused for a while. Wait a moment
  and try again.

## Signing out

1. Select **Sign out** in the header at the top of the screen.
2. Signing out ends your session completely — you will need a fresh code
   next time.

> ⚠️ Always sign out on shared or public computers. Everything you do in the
> console is recorded under your name.
