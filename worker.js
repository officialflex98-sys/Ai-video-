export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    let update;
    try {
      update = await request.json();
    } catch (e) {
      return new Response("Bad request", { status: 400 });
    }

    const message = update.message;
    if (!message) {
      return new Response("OK", { status: 200 });
    }

    const chatId = String(message.chat?.id || "");
    if (chatId !== env.TELEGRAM_CHAT_ID) {
      // Ignore anyone who isn't you - no reply at all, so the bot
      // doesn't confirm to strangers that it's listening.
      return new Response("OK", { status: 200 });
    }

    // Immediate acknowledgement, before anything else happens.
    await sendTelegramMessage(env, chatId, "Accepted");

    const document = message.document;
    if (!document) {
      await sendTelegramMessage(
        env, chatId,
        "Please send your script as a .txt file attachment, not a typed message."
      );
      return new Response("OK", { status: 200 });
    }

    const fileName = document.file_name || "";
    if (!fileName.toLowerCase().endsWith(".txt")) {
      await sendTelegramMessage(
        env, chatId,
        `Received '${fileName}' - only .txt files are accepted, ignoring.`
      );
      return new Response("OK", { status: 200 });
    }

    try {
      const fileBuffer = await downloadTelegramFile(env, document.file_id);
      await commitScriptToGitHub(env, fileBuffer);
      await sendTelegramMessage(env, chatId, "Overwritten - wait a moment, generating your video now.");
    } catch (err) {
      await sendTelegramMessage(env, chatId, `Something went wrong updating the script: ${err.message}`);
    }

    return new Response("OK", { status: 200 });
  },
};

async function sendTelegramMessage(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}

async function downloadTelegramFile(env, fileId) {
  const fileInfoResp = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getFile?file_id=${fileId}`
  );
  const fileInfo = await fileInfoResp.json();
  const filePath = fileInfo.result.file_path;

  const fileResp = await fetch(
    `https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${filePath}`
  );
  return await fileResp.arrayBuffer();
}

async function commitScriptToGitHub(env, fileBuffer) {
  const apiUrl = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/contents/script.txt`;

  // GitHub requires the current file's SHA to overwrite it.
  let sha;
  const getResp = await fetch(apiUrl, {
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "User-Agent": "telegram-script-sync-worker",
      Accept: "application/vnd.github+json",
    },
  });
  if (getResp.ok) {
    sha = (await getResp.json()).sha;
  }

  const putResp = await fetch(apiUrl, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "User-Agent": "telegram-script-sync-worker",
      Accept: "application/vnd.github+json",
    },
    body: JSON.stringify({
      message: "Update script.txt via Telegram",
      content: arrayBufferToBase64(fileBuffer),
      sha: sha,
    }),
  });

  if (!putResp.ok) {
    throw new Error(`GitHub commit failed: ${putResp.status} ${await putResp.text()}`);
  }
}

function arrayBufferToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}
