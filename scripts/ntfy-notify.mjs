#!/usr/bin/env node
/**
 * scripts/ntfy-notify.mjs
 *
 * שולח הודעת push דרך ntfy.sh. משמש עבור כל התראת מידע שלא דורשת תשובת
 * reply מהמשתמש (סיכום ריצה, תוצאה אחרי אישור/דחייה/שינוי, דיווח תקלה) —
 * ראו "עדכון ערוץ התראות — ntfy במקום Remote Control" ב-
 * agents/whatsapp-event-calendar-agent.md.
 *
 * לא משמש עבור הצעת אירוע חדשה — זו יוצאת כהודעת WhatsApp אמיתית
 * (POST /api/sendText לצ'אט העצמי), כי רק שם אפשר לתפוס תשובת reply.
 *
 * שימוש:
 *   node scripts/ntfy-notify.mjs --title "כותרת" --message "טקסט ההודעה" [--priority urgent] [--tags x]
 *
 * משתני סביבה:
 *   NTFY_TOPIC - חובה. שם ה-topic (למשל ami-whatsapp-agent-x7k2p).
 *
 * קוד יציאה: 0 בהצלחה, 1 בכשל (כשל בשליחת ההתראה עצמה לא אמור להפיל את
 * שאר הריצה - הקורא אחראי להחליט אם להתייחס לזה כשגיאה חוסמת).
 */

function parseArgs(argv) {
  const args = { priority: "default", tags: "" };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (key === "--title") args.title = argv[++i];
    else if (key === "--message") args.message = argv[++i];
    else if (key === "--priority") args.priority = argv[++i];
    else if (key === "--tags") args.tags = argv[++i];
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (!args.title || !args.message) {
    console.error(
      JSON.stringify({
        ok: false,
        error: "חסרים פרמטרים חובה: --title ו---message",
      })
    );
    process.exit(1);
  }

  const topic = process.env.NTFY_TOPIC;
  if (!topic) {
    console.error(
      JSON.stringify({
        ok: false,
        error: "משתנה הסביבה NTFY_TOPIC לא מוגדר",
      })
    );
    process.exit(1);
  }

  const params = new URLSearchParams();
  params.set("title", args.title);
  params.set("priority", args.priority);
  if (args.tags) params.set("tags", args.tags);

  const url = `https://ntfy.sh/${encodeURIComponent(topic)}?${params.toString()}`;
  const headers = {
    "Content-Type": "text/plain; charset=utf-8",
  };

  try {
    const res = await fetch(url, {
      method: "POST",
      headers,
      body: args.message,
    });

    if (!res.ok) {
      const bodyText = await res.text().catch(() => "");
      console.error(
        JSON.stringify({
          ok: false,
          error: `ntfy החזיר סטטוס ${res.status}`,
          details: bodyText,
        })
      );
      process.exit(1);
    }

    console.log(JSON.stringify({ ok: true, topic, title: args.title }));
    process.exit(0);
  } catch (err) {
    console.error(
      JSON.stringify({
        ok: false,
        error: `כשל בקריאת HTTP ל-ntfy: ${err.message}`,
      })
    );
    process.exit(1);
  }
}

main();